# Copyright (c) 2023, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys
import gc
import json
import time
import wandb
import pynvml
import numpy as np

import torch
import torch.cuda.amp as amp
import torch.distributed as dist

from tqdm import tqdm
from apex import optimizers
from collections import OrderedDict

import visualize

from models import get_model
from modulus.models.sfno.preprocessor import get_preprocessor
from modulus.datapipes.climate.sfno.dataloader import get_dataloader

from modulus.utils.sfno.distributed import comm
from modulus.utils.sfno.loss import LossHandler
from modulus.utils.sfno.metric import MetricsHandler
from modulus.utils.sfno.distributed.helpers import sync_params
from modulus.utils.sfno.distributed.mappings import init_gradient_reduction_hooks

from helpers import count_parameters

from modulus.launch.logging import (
    PythonLogger,
    LaunchLogger,
    initialize_wandb,
    RankZeroLoggingWrapper,
)


class Trainer:
    # jit stuff
    def _compile_model(self, inp_shape):
        if self.params.jit_mode == "script":
            if dist.is_initialized() and not self.params.disable_ddp:
                self.model.module = torch.jit.script(self.model.module)
            else:
                self.model = torch.jit.script(self.model)
            self.model_train = self.model
            self.model_eval = self.model

        elif self.params.jit_mode == "inductor":
            self.model = torch.compile(self.model)
            self.model_train = self.model
            self.model_eval = self.model

        else:
            self.model_train = self.model
            self.model_eval = self.model

        return

    # graph stuff
    def _capture_model(self, capture_stream, inp_shape, tar_shape, num_warmup_steps=20):
        matmul_comm_size = comm.get_size("matmul")

        # modify inp shape due to model parallelism
        if self.params.split_data_channels:
            inp_shape_eff = (
                inp_shape[0],
                (inp_shape[1] + matmul_comm_size - 1) // matmul_comm_size,
                inp_shape[2],
                inp_shape[3],
            )

            tar_shape_eff = (
                tar_shape[0],
                (tar_shape[1] + matmul_comm_size - 1) // matmul_comm_size,
                tar_shape[2],
                tar_shape[3],
            )
        else:
            inp_shape_eff = (inp_shape[0], inp_shape[1], inp_shape[2], inp_shape[3])

            tar_shape_eff = (tar_shape[0], tar_shape[1], tar_shape[2], tar_shape[3])

        self.static_inp = torch.zeros(
            inp_shape_eff, dtype=torch.float32, device=self.device
        )
        self.static_tar = torch.zeros(
            tar_shape_eff, dtype=torch.float32, device=self.device
        )

        if self.params.enable_nhwc:
            self.static_inp = self.static_inp.to(memory_format=torch.channels_last)
            self.static_tar = self.static_tar.to(memory_format=torch.channels_last)

        # set to train
        self._set_train()

        # do capture
        if capture_stream is None:
            capture_stream = torch.cuda.Stream()
        capture_stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(capture_stream):
            for _ in range(num_warmup_steps):
                self.model_train.zero_grad(set_to_none=True)

                # FW
                with amp.autocast(enabled=self.amp_enabled, dtype=self.amp_dtype):
                    self.static_pred = self.model_train(self.static_inp).to(self.device)
                    self.static_loss = self.loss_obj(
                        self.static_pred, self.static_tar, self.static_inp
                    )

                # BW
                self.gscaler.scale(self.static_loss).backward()

            # sync here
            capture_stream.synchronize()

            gc.collect()
            torch.cuda.empty_cache()

            # create graph
            self.graph = torch.cuda.CUDAGraph()

            # zero grads before capture:
            self.model_train.zero_grad(set_to_none=True)

            # start capture
            self.graph.capture_begin()

            # FW
            with amp.autocast(enabled=self.amp_enabled, dtype=self.amp_dtype):
                self.static_pred = self.model_train(self.static_inp)
                self.static_loss = self.loss_obj(
                    self.static_pred, self.static_tar, self.static_inp
                )

            # BW
            self.gscaler.scale(self.static_loss).backward()

            # end capture
            self.graph.capture_end()

        torch.cuda.current_stream().wait_stream(capture_stream)

        return

    def _get_time_stats(self):
        # get some stats: make data shared with tensor from the class
        _, out_scale = self.train_dataloader.get_output_normalization()
        mult_cpu = torch.from_numpy(out_scale)[0, :, 0, 0]

        # compute
        if self.params.enable_synthetic_data:
            clim = torch.zeros(
                [
                    self.params.N_out_channels,
                    self.params.img_crop_shape_x,
                    self.params.img_crop_shape_y,
                ],
                dtype=torch.float32,
                device=self.device,
            )

        else:
            # full bias and scale
            in_bias, in_scale = self.train_dataloader.get_input_normalization()
            in_bias = in_bias[
                0, ...
            ]  # np.load(self.params.global_means_path)[0, self.params.out_channels]
            in_scale = in_scale[
                0, ...
            ]  # np.load(self.params.global_stds_path)[0, self.params.out_channels]

            # we need this window
            start_x = self.params.img_crop_offset_x
            end_x = start_x + self.params.img_crop_shape_x
            start_y = self.params.img_crop_offset_y
            end_y = start_y + self.params.img_crop_shape_y

            # now we crop the time means
            time_means = np.load(self.params.time_means_path)[
                0, self.params.out_channels, start_x:end_x, start_y:end_y
            ]
            clim = torch.as_tensor(
                (time_means - in_bias) / in_scale, dtype=torch.float32
            )

        return mult_cpu, clim

    def _update_parameters(self, params):
        """
        This could be moved potentially. The idea is to process params and handle the logics for params
        """

        params.in_channels = self.valid_dataset.in_channels
        params.N_in_channels = len(self.valid_dataset.in_channels)
        params.out_channels = self.valid_dataset.out_channels
        params.N_out_channels = len(self.valid_dataset.out_channels)

        params.img_shape_x = self.valid_dataset.img_shape_x
        params.img_shape_y = self.valid_dataset.img_shape_y

        params.img_crop_shape_x = self.valid_dataset.img_crop_shape_x
        params.img_crop_shape_y = self.valid_dataset.img_crop_shape_y
        params.img_crop_offset_x = self.valid_dataset.img_crop_offset_x
        params.img_crop_offset_y = self.valid_dataset.img_crop_offset_y

        params.img_local_shape_x = self.valid_dataset.img_local_shape_x
        params.img_local_shape_y = self.valid_dataset.img_local_shape_y
        params.img_local_offset_x = self.valid_dataset.img_local_offset_x
        params.img_local_offset_y = self.valid_dataset.img_local_offset_y

        # derived quantities
        params["N_in_predicted_channels"] = params.N_in_channels

        # sanitization:
        if not hasattr(params, "add_zenith"):
            params["add_zenith"] = False

        # input channels
        # zenith channel is appended to all the samples, so we need to do it here
        if params.add_zenith:
            params.N_in_channels += 1

        if params.n_history >= 1:
            params.N_in_channels = (params.n_history + 1) * params.N_in_channels
            params.N_in_predicted_channels *= params.n_history + 1

        # these are static and the same for all samples in the same time history
        if params.add_grid:
            n_grid_chan = 2
            if (params.gridtype == "sinusoidal") and hasattr(
                params, "grid_num_frequencies"
            ):
                n_grid_chan *= params.grid_num_frequencies
            params.N_in_channels += n_grid_chan

        if params.add_orography:
            params.N_in_channels += 1

        if params.add_landmask:
            params.N_in_channels += 2

        # target channels
        params.N_target_channels = (params.n_future + 1) * params.N_out_channels

        # MISC parameters
        if not hasattr(params, "history_normalization_mode"):
            params["history_normalization_mode"] = "none"

        if not hasattr(params, "multigrid_mode"):
            params["multigrid_mode"] = "none"

        if not hasattr(params, "num_visualization_workers"):
            params["num_visualization_workers"] = 1

        if not hasattr(params, "log_video"):
            params["log_video"] = 0

        # automatically detect wind channels and keep track of them
        if hasattr(params, "channel_names") and not hasattr(params, "wind_channels"):
            channel_names = params.channel_names
            channel_dict = {
                channel_names[ch]: ch
                for ch in set(params.in_channels + params.out_channels)
            }
            wind_channels = []
            for chn, ch in channel_dict.items():
                if chn[0] == "u":
                    vchn = "v" + chn[1:]
                    if vchn in channel_dict.keys():
                        # wind_channels.append(ch, channel_dict[vchn])
                        wind_channels = wind_channels + [ch, channel_dict[vchn]]
            params["wind_channels"] = wind_channels

        if not hasattr(params, "load_checkpoint"):
            params["load_checkpoint"] = "legacy"
        if not hasattr(params, "save_checkpoint"):
            params["save_checkpoint"] = "legacy"

        return params

    def __del__(self):
        if self.params.log_to_wandb:
            wandb.finish()

    def __init__(self, params, world_rank):
        self.params = None
        self.world_rank = world_rank
        self.rank = world_rank
        self.data_parallel_rank = comm.get_rank("data")
        if torch.cuda.is_available():
            self.device = torch.device(f"cuda:{torch.cuda.current_device()}")
        else:
            self.device = torch.device("cpu")

        LaunchLogger.initialize()
        self.logger = PythonLogger("main")  # General python logger
        # add back in when logger working
        # if self.world_rank == 0:
        #    self.logger.file_logging(file_name=os.path.join(params.experiment_dir, "out.log"))
        self.rank_zero_logger = RankZeroLoggingWrapper(self.logger, self)

        # nvml stuff
        if params.log_to_screen:
            pynvml.nvmlInit()
            self.nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(self.device.index)

        # set amp_parameters
        self.amp_enabled = params.amp_mode != "none"
        self.amp_dtype = (
            torch.float16
            if (params.amp_mode == "fp16")
            else torch.bfloat16
            if (params.amp_mode == "bf16")
            else None
        )

        if params.log_to_wandb:
            # login first:
            wandb.login()
            # init
            wandb.init(
                dir=params.experiment_dir,
                config=params,
                name=params.wandb_name,  # if not params.resuming else None,
                group=params.wandb_group,  # if not params.resuming else None,
                project=params.wandb_project,
                entity=params.wandb_entity,
                resume=params.resuming,
            )

        # data loader
        self.rank_zero_logger.info("initializing data loader")

        self.train_dataloader, self.train_dataset, self.train_sampler = get_dataloader(
            params, params.train_data_path, train=True, device=self.device
        )

        self.valid_dataloader, self.valid_dataset = get_dataloader(
            params, params.valid_data_path, train=False, device=self.device
        )

        self.rank_zero_logger.info("data loader initialized")

        # update params
        params = self._update_parameters(params)

        # save params
        self.params = params

        # init preprocessor and model

        # save the modified params to a json file to make it easier to load for inference later on
        # This should happen immediately before ``get_model`` is called.
        if self.world_rank == 0:
            config_path = os.path.join(params.experiment_dir, "config.json")
            with open(config_path, "w") as f:
                json.dump(params.to_dict(), f)

        self.model = get_model(params).to(self.device)
        self.preprocessor = self.model.preprocessor

        # if model-parallelism is enabled, we need to sure that shared weights are matching across ranks
        # as random seeds might get out of sync during initialization
        if comm.get_size("model") > 1:
            sync_params(self.model, mode="broadcast")

        # define process group for DDP, we might need to override that
        if dist.is_initialized() and not params.disable_ddp:
            ddp_process_group = comm.get_group("data")

        if params.log_to_wandb:
            wandb.watch(self.model)

        # print model
        if self.world_rank == 0:
            print(self.model)

        # metrics handler
        mult_cpu, clim = self._get_time_stats()
        self.metrics = MetricsHandler(self.params, mult_cpu, clim, self.device)
        self.metrics.initialize_buffers()

        # loss handler
        self.loss_obj = LossHandler(self.params, d=2)
        self.loss_obj = self.loss_obj.to(self.device)
        if self.params.enable_nhwc:
            self.loss_obj = self.loss_obj.to(memory_format=torch.channels_last)

        if not params.resuming:
            if params.nettype == "unet":
                self.model.apply(self.model.get_weights_function(params.weight_init))

        self.capturable_optimizer = False
        betas = (params.optimizer_beta1, params.optimizer_beta2)
        if params.optimizer_type == "FusedAdam":
            self.rank_zero_logger.info("using FusedAdam")
            self.optimizer = optimizers.FusedAdam(
                self.model.parameters(),
                betas=betas,
                lr=params.lr,
                weight_decay=params.weight_decay,
            )
        elif params.optimizer_type == "FusedLAMB":
            try:
                from apex.optimizers import FusedMixedPrecisionLamb

                self.rank_zero_logger.info("using FusedMixedPrecisionLamb")
                self.optimizer = FusedMixedPrecisionLamb(
                    self.model.parameters(),
                    betas=betas,
                    lr=params.lr,
                    weight_decay=params.weight_decay,
                    max_grad_norm=params.optimizer_max_grad_norm,
                )
                self.capturable_optimizer = True
            except ImportError:
                self.rank_zero_logger.info("using FusedLAMB")
                self.optimizer = optimizers.FusedLAMB(
                    self.model.parameters(),
                    betas=betas,
                    lr=params.lr,
                    weight_decay=params.weight_decay,
                    max_grad_norm=params.optimizer_max_grad_norm,
                )
        elif params.optimizer_type == "Adam":
            self.rank_zero_logger.info("using Adam")
            self.optimizer = torch.optim.Adam(self.model.parameters(), lr=params.lr)
        elif params.optimizer_type == "SGD":
            self.rank_zero_logger.info("using SGD")
            self.optimizer = torch.optim.SGD(
                self.model.parameters(),
                lr=params.lr,
                weight_decay=params.weight_decay,
                momentum=0,
            )
        else:
            raise ValueError(f"Unknown optimizer type {params.optimizer_type}")

        if params.scheduler == "ReduceLROnPlateau":
            self.scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
                self.optimizer, factor=0.2, patience=5, mode="min"
            )
        elif params.scheduler == "CosineAnnealingLR":
            if not hasattr(params, "scheduler_min_lr"):
                params["scheduler_min_lr"] = 0.0
            self.scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
                self.optimizer,
                T_max=params.scheduler_T_max,
                eta_min=params.scheduler_min_lr,
            )
        elif params.scheduler == "OneCycleLR":
            self.scheduler = torch.optim.lr_scheduler.OneCycleLR(
                self.optimizer,
                max_lr=params.lr,
                total_steps=params.scheduler_T_max,
                steps_per_epoch=1,
            )
        else:
            self.scheduler = None
        if params.lr_warmup_steps > 0:
            from utils.warmup_scheduler import WarmupScheduler

            self.scheduler = WarmupScheduler(
                self.scheduler,
                num_warmup_steps=params.lr_warmup_steps,
                start_lr=params.lr_start,
            )

        self.gscaler = amp.GradScaler(enabled=(self.amp_dtype == torch.float16))

        # we need this further down
        capture_stream = None
        if dist.is_initialized() and not params.disable_ddp:
            capture_stream = torch.cuda.Stream()
            parameter_size_mb = (
                count_parameters(self.model, self.device) * 4 / float(1024 * 1024)
            )
            reduction_size_mb = int(
                (parameter_size_mb / params.parameters_reduction_buffer_count) * 1.05
            )
            with torch.cuda.stream(capture_stream):
                self.model = init_gradient_reduction_hooks(
                    self.model,
                    device_ids=[self.device.index],
                    output_device=[self.device.index],
                    bucket_cap_mb=reduction_size_mb,
                    broadcast_buffers=True,
                    find_unused_parameters=False,
                    gradient_as_bucket_view=True,
                    static_graph=params.checkpointing > 0,
                )
                capture_stream.synchronize()

            # capture stream sync
            capture_stream.synchronize()

        # lets get one sample from the dataloader:
        # get sample and map to gpu
        iterator = iter(self.train_dataloader)
        data = next(iterator)
        gdata = map(lambda x: x.to(self.device, dtype=torch.float32), data)
        # extract unpredicted features
        inp, tar = self.preprocessor.cache_unpredicted_features(*gdata)
        # flatten
        inp = self.preprocessor.flatten_history(inp)
        tar = self.preprocessor.flatten_history(tar)
        # get shapes
        inp_shape = inp.shape
        tar_shape = tar.shape
        self._compile_model(inp_shape)
        if not self.loss_obj.is_distributed():
            self.loss_obj = torch.jit.script(self.loss_obj)

        # graph capture
        self.graph = None
        if params.cuda_graph_mode != "none":
            self._capture_model(
                capture_stream, inp_shape, tar_shape, num_warmup_steps=20
            )

        # visualization wrapper:
        plot_list = [
            {
                "name": "windspeed_uv10",
                "functor": "lambda x: np.sqrt(np.square(x[0, ...]) + np.square(x[1, ...]))",
                "diverging": False,
            }
        ]
        out_bias, out_scale = self.train_dataloader.get_output_normalization()
        self.visualizer = visualize.VisualizationWrapper(
            params.log_to_wandb,
            path=None,
            prefix=None,
            plot_list=plot_list,
            scale=out_scale[0, ...],
            bias=out_bias[0, ...],
            num_workers=params.num_visualization_workers,
        )
        # allocate pinned tensors for faster copy:
        self.viz_stream = torch.cuda.Stream()
        self.viz_prediction_cpu = torch.empty(
            (
                (params.N_target_channels // (params.n_future + 1)),
                params.img_shape_x,
                params.img_shape_y,
            ),
            device="cpu",
        ).pin_memory()
        self.viz_target_cpu = torch.empty(
            (
                (params.N_target_channels // (params.n_future + 1)),
                params.img_shape_x,
                params.img_shape_y,
            ),
            device="cpu",
        ).pin_memory()

        # reload checkpoints
        self.iters = 0
        self.startEpoch = 0
        if params.finetune and not params.resuming:
            assert (
                params.pretrained_checkpoint_path is not None
            ), "Error, please specify a valid pretrained checkpoint path"
            self.restore_checkpoint(
                params.pretrained_checkpoint_path,
                checkpoint_mode=params["load_checkpoint"],
            )

        if params.resuming:
            self.restore_checkpoint(
                params.checkpoint_path, checkpoint_mode=params["load_checkpoint"]
            )

        self.epoch = self.startEpoch

        # counting runs a reduction so we need to count on all ranks before printing on rank 0
        pcount = count_parameters(self.model, self.device)
        if params.log_to_screen:
            self.rank_zero_logger.info(
                f"Number of trainable model parameters: {pcount}"
            )

    def train(self):
        # log parameters
        if self.params.log_to_screen:
            # log memory usage so far
            all_mem_gb = pynvml.nvmlDeviceGetMemoryInfo(self.nvml_handle).used / (
                1024.0 * 1024.0 * 1024.0
            )
            max_mem_gb = torch.cuda.max_memory_allocated(device=self.device) / (
                1024.0 * 1024.0 * 1024.0
            )
            self.rank_zero_logger.info(
                f"Scaffolding memory high watermark: {all_mem_gb} GB ({max_mem_gb} GB for pytorch)"
            )
            # announce training start
            self.rank_zero_logger.info("Starting Training Loop...")

        # perform a barrier here to make sure everybody is ready
        if dist.is_initialized():
            dist.barrier(device_ids=[self.device.index])

        try:
            torch.cuda.reset_peak_memory_stats(self.device)
        except ValueError:
            pass

        training_start = time.time()
        best_valid_loss = 1.0e6
        for epoch in range(self.startEpoch, self.params.max_epochs):
            if dist.is_initialized() and self.train_sampler is not None:
                self.train_sampler.set_epoch(epoch)

            # start timer
            epoch_start = time.time()
            train_time, train_data_gb, train_logs = self.train_one_epoch()
            valid_time, viz_time, valid_logs = self.validate_one_epoch(epoch)

            # if epoch == self.params.max_epochs - 1:
            #     self.train_dataloader.reset_pipeline()
            #     self.valid_dataloader.reset_pipeline()
            #     inf_time, inf_logs = self.inference_one_epoch(epoch)

            if self.params.scheduler == "ReduceLROnPlateau":
                self.scheduler.step(valid_logs["base"]["validation loss"])
            elif self.scheduler is not None:
                self.scheduler.step()

            if self.params.log_to_wandb:
                for pg in self.optimizer.param_groups:
                    lr = pg["lr"]
                wandb.log({"learning rate": lr}, step=self.epoch)

            if (self.data_parallel_rank == 0) and self.params.save_checkpoint:
                # checkpoint at the end of every epoch
                self.save_checkpoint(
                    self.params.checkpoint_path,
                    checkpoint_mode=self.params["save_checkpoint"],
                )
                best_checkpoint_path = self.params.best_checkpoint_path.format(
                    mp_rank=comm.get_rank("model")
                )
                best_checkpoint_saved = os.path.isfile(best_checkpoint_path)
                if (not best_checkpoint_saved) or valid_logs["base"][
                    "validation loss"
                ] <= best_valid_loss:
                    # logging.info('Val loss improved from {} to {}'.format(best_valid_loss, valid_logs['valid_loss']))
                    self.save_checkpoint(
                        self.params.best_checkpoint_path,
                        checkpoint_mode=self.params["save_checkpoint"],
                    )
                    best_valid_loss = valid_logs["base"]["validation loss"]

            # wait for everybody
            if dist.is_initialized():
                dist.barrier(device_ids=[self.device.index])

            # end timer
            epoch_end = time.time()

            # create timing logs:
            timing_logs = {
                "epoch time [s]": epoch_end - epoch_start,
                "training time [s]": train_time,
                "validation time [s]": valid_time,
                "visualization time [s]": viz_time,
                "training step time [ms]": (train_time / train_logs["train_steps"])
                * 10**3,
                "minimal IO rate [GB/s]": train_data_gb / train_time,
            }

            # log metrics:
            self.log_epoch(train_logs, valid_logs, timing_logs)

        # training done
        training_end = time.time()
        if self.params.log_to_screen:
            self.rank_zero_logger.success(
                "Total training time is {:.2f} sec".format(
                    training_end - training_start
                )
            )

        return

    def _set_train(self):
        self.model.train()
        self.loss_obj.train()
        self.preprocessor.train()

    def _set_eval(self):
        self.model.eval()
        self.loss_obj.eval()
        self.preprocessor.eval()

    def train_one_epoch(self):
        self.epoch += 1
        total_data_bytes = 0
        self._set_train()

        train_steps = 0
        train_start = time.perf_counter_ns()
        for data in tqdm(
            self.train_dataloader,
            desc="Training progress  ",
            disable=not self.params.log_to_screen,
        ):
            train_steps += 1
            self.iters += 1

            # map to device
            gdata = map(lambda x: x.to(self.device, dtype=torch.float32), data)

            # do preprocessing
            inp, tar = self.preprocessor.cache_unpredicted_features(*gdata)
            inp = self.preprocessor.flatten_history(inp)
            tar = self.preprocessor.flatten_history(tar)

            # assuming float32
            total_data_bytes += (torch.numel(inp) + torch.numel(tar)) * 4

            if self.graph is not None:
                self.static_inp.copy_(inp)
                self.static_tar.copy_(tar)
                self.graph.replay()
                loss = self.static_loss
            else:
                self.model_train.zero_grad(set_to_none=True)
                with amp.autocast(enabled=self.amp_enabled, dtype=self.amp_dtype):
                    pred = self.model_train(inp)
                    loss = self.loss_obj(pred, tar, inp)

                self.gscaler.scale(loss).backward()

            # perform weight update
            self.gscaler.step(self.optimizer)
            self.gscaler.update()

            if (
                (self.params.print_timings_frequency > 0)
                and (self.iters % self.params.print_timings_frequency == 0)
                and self.params.log_to_screen
            ):
                running_train_time = time.perf_counter_ns() - train_start
                print(
                    f"Average step time after step {self.iters}: {running_train_time / float(train_steps) * 10**(-6):.1f} ms"
                )
                print(
                    f"Average effective io rate after step {self.iters}: {total_data_bytes * float(comm.get_world_size()) / (float(running_train_time) * 10**(-9) * 1024. * 1024. * 1024.):.2f} GB/s"
                )
                print(f"Current loss {loss.item()}")

        # add the eval loss to logs
        logs = {"loss": loss}

        if dist.is_initialized():
            for key in sorted(logs.keys()):
                dist.all_reduce(
                    logs[key].detach(),
                    op=dist.ReduceOp.AVG,
                    group=comm.get_group("data"),
                )
                logs[key] = logs[key].item()

        # add train steps to log
        logs["train_steps"] = train_steps

        # global sync is in order
        if dist.is_initialized():
            dist.barrier(device_ids=[self.device.index])

        # finalize timers
        train_end = time.perf_counter_ns()
        train_time = (train_end - train_start) * 10 ** (-9)
        total_data_gb = (total_data_bytes / (1024.0 * 1024.0 * 1024.0)) * float(
            comm.get_world_size()
        )

        return train_time, total_data_gb, logs

    def validate_one_epoch(self, epoch):
        # set to eval
        self._set_eval()

        # clear cache
        torch.cuda.empty_cache()

        # initialize metrics buffers
        self.metrics.zero_buffers()

        visualize = self.params.log_video and (epoch % self.params.log_video == 0)

        # start the timer
        valid_start = time.time()

        with torch.inference_mode():
            with torch.no_grad():
                eval_steps = 0
                for data in tqdm(
                    self.valid_dataloader,
                    desc="Validation progress",
                    disable=not self.params.log_to_screen,
                ):
                    eval_steps += 1

                    # map to gpu
                    gdata = map(lambda x: x.to(self.device, dtype=torch.float32), data)

                    # preprocess
                    inp, tar = self.preprocessor.cache_unpredicted_features(*gdata)
                    inp = self.preprocessor.flatten_history(inp)

                    # split list of targets
                    tarlist = torch.split(tar, 1, dim=1)
                    inpt = inp

                    # do autoregression
                    for idt, targ in enumerate(tarlist):

                        # flatten history of the target
                        targ = self.preprocessor.flatten_history(targ)

                        # FW pass
                        with amp.autocast(
                            enabled=self.amp_enabled, dtype=self.amp_dtype
                        ):
                            pred = self.model_eval(inpt)
                            loss = self.loss_obj(pred, targ, inpt)

                            if eval_steps <= 1 and visualize:
                                pred_single = pred[0:1, ...].clone()
                                targ_single = targ[0:1, ...].clone()
                                pred_gather = torch.squeeze(
                                    self.metrics._gather_input(pred_single), dim=0
                                )
                                targ_gather = torch.squeeze(
                                    self.metrics._gather_input(targ_single), dim=0
                                )
                                self.viz_stream.wait_stream(torch.cuda.current_stream())
                                with torch.cuda.stream(self.viz_stream):
                                    self.viz_prediction_cpu.copy_(
                                        pred_gather, non_blocking=True
                                    )
                                    self.viz_target_cpu.copy_(
                                        targ_gather, non_blocking=True
                                    )
                                self.viz_stream.synchronize()

                                pred_cpu = self.viz_prediction_cpu.to(
                                    torch.float32
                                ).numpy()
                                targ_cpu = self.viz_target_cpu.to(torch.float32).numpy()

                                tag = f"step{eval_steps}_time{str(idt).zfill(3)}"
                                self.visualizer.add(tag, pred_cpu, targ_cpu)

                        # put in the metrics handler
                        self.metrics.update(pred, targ, loss, idt)

                        # append history
                        inpt = self.preprocessor.append_history(inpt, pred, idt)

        # create final logs
        logs = self.metrics.finalize()

        # finalize plotting
        viz_time = time.perf_counter_ns()
        if visualize:
            self.visualizer.finalize()
        viz_time = (time.perf_counter_ns() - viz_time) * 10 ** (-9)

        # global sync is in order
        if dist.is_initialized():
            dist.barrier(device_ids=[self.device.index])

        # timer
        valid_time = time.time() - valid_start

        return valid_time, viz_time, logs

    def inference_one_epoch(self, epoch):
        self._set_eval()

        self.inference_dataloader, self.inference_dataset = get_dataloader(
            self.params,
            self.params.inf_data_path,
            train=False,
            final_eval=True,
            device=self.device,
        )

        self.metrics.initialize_buffers()
        # start the timer
        valid_start = time.time()

        with torch.no_grad():
            eval_steps = 0
            for data in tqdm(
                self.inference_dataloader,
                desc="Inference progress",
                disable=not self.params.log_to_screen,
            ):
                eval_steps += 1

                gdata = map(lambda x: x.to(self.device, dtype=torch.float32), data)

                if len(data) == 4:
                    inp, tar, izen, tzen = gdata
                    tzenlist = torch.split(tzen, 1, dim=1)
                else:
                    inp, tar = gdata
                    izen = None
                    tzenlist = None

                # split list of targets
                tarlist = torch.split(tar, 1, dim=1)
                inpt = inp
                for idt, targ in enumerate(tarlist):
                    # might modify inpt too often
                    inpt, targ = self.preprocessor(inpt, targ, izen)

                    # FW pass
                    with amp.autocast(enabled=self.amp_enabled, dtype=self.amp_dtype):
                        pred = self.model_eval(inpt)
                        loss = self.loss_obj(pred, targ, inpt)

                        # append zenith angle to prediction
                        if tzenlist is not None:
                            predt = self.preprocessor.append_channels(
                                pred, tzenlist[idt]
                            )
                        else:
                            predt = pred

                    # append history if requested # does this even do anything here?????
                    inpt = self.preprocessor.append_history(inpt, predt)
                    # set to none so that we no not re-attach the channels
                    izen = None

                    # put in the metrics handler
                    self.metrics.update(pred, targ, loss, idt)

        # create final logs
        logs, acc_curve = self.metrics.finalize(final_inference=True)

        if self.world_rank == 0:
            np.save(
                os.path.join(self.params.experiment_dir, "acc_curve.npy"),
                acc_curve.cpu().numpy(),
            )

            if self.params.ifs_acc_path is not None:
                visualize.plot_ifs_acc_comparison(acc_curve, self.params, self.epoch)

        # global sync is in order
        if dist.is_initialized():
            dist.barrier(device_ids=[self.device.index])

        # timer
        inference_time = time.time() - valid_start

        return inference_time, logs

    def test_model_output(self, model):
        """helper to test checkpointing"""
        inp_shape = (
            self.params.batch_size,
            self.params.N_in_channels,
            self.params.img_shape_local_x,
            self.params.img_shape_local_y,
        )
        matmul_comm_size = comm.get_size("matmul")

        # modify inp shape due to model parallelism
        if self.params.split_data_channels:
            inp_shape_eff = (
                inp_shape[0],
                (inp_shape[1] + matmul_comm_size - 1) // matmul_comm_size,
                inp_shape[2],
                inp_shape[3],
            )
        else:
            inp_shape_eff = (inp_shape[0], inp_shape[1], inp_shape[2], inp_shape[3])

        random_tensor = os.path.join(
            self.params.experiment_dir,
            "random_tensor{}.npy".format(comm.get_rank("model")),
        )
        if not os.path.exists(random_tensor):
            y = torch.rand(inp_shape_eff, dtype=torch.float).cpu().numpy()
            np.save(random_tensor, y)

        y = torch.from_numpy(np.load(random_tensor)).type(torch.float).to(self.device)
        out = model(y).detach().cpu().numpy()
        random_output = os.path.join(
            self.params.experiment_dir,
            "random_output{}.npy".format(comm.get_rank("model")),
        )
        if os.path.exists(random_output):
            out_old = np.load(random_output)
            diff = (out - out_old).flatten()
            self.rank_zero_logger.info(
                "Diff metrics: norm = {}, max = {}, min = {}".format(
                    np.linalg.norm(diff), np.max(diff), np.min(diff)
                )
            )
        np.save(random_output, out)

    def log_epoch(self, train_logs, valid_logs, timing_logs):
        # separator
        separator = "".join(["-" for _ in range(50)])
        print_prefix = "    "

        def get_pad(nchar):
            return "".join([" " for x in range(nchar)])

        if self.params.log_to_screen:
            # header:
            self.rank_zero_logger.info(separator)
            self.rank_zero_logger.info(f"Epoch {self.epoch} summary:")
            self.rank_zero_logger.info(f"Performance Parameters:")
            self.rank_zero_logger.info(
                print_prefix + "training steps: {}".format(train_logs["train_steps"])
            )
            self.rank_zero_logger.info(
                print_prefix
                + "validation steps: {}".format(valid_logs["base"]["validation steps"])
            )
            all_mem_gb = pynvml.nvmlDeviceGetMemoryInfo(self.nvml_handle).used / (
                1024.0 * 1024.0 * 1024.0
            )
            self.rank_zero_logger.info(
                print_prefix + f"memory footprint [GB]: {all_mem_gb:.2f}"
            )
            for key in timing_logs.keys():
                self.rank_zero_logger.info(
                    print_prefix + key + ": {:.2f}".format(timing_logs[key])
                )

            # logging.info('Time taken for training in epoch {} is {:.2f} sec ({} steps)'.format(epoch + 1, time.time()-start, train_logs["train_steps"]))
            # logging.info('Time taken for validation in epoch {} is {:.2f} sec ({} steps)'.format(epoch + 1, valid_time, valid_logs['base']["validation steps"]))
            # logging.info('Effective training IO rate for epoch {} is {:.2f} GB/s'.format(epoch + 1, train_data_gb/tr_time))
            # all_mem_gb = pynvml.nvmlDeviceGetMemoryInfo(self.nvml_handle).used / (1024. * 1024. * 1024.)
            # max_mem_gb = torch.cuda.max_memory_allocated(device=self.device) / (1024. * 1024. * 1024.)
            # logging.info(f'Memory high watermark: {all_mem_gb:.2f} GB ({max_mem_gb:.2f} GB for pytorch)')

            # compute padding:
            print_list = ["training loss", "validation loss", "validation L1"] + list(
                valid_logs["metrics"].keys()
            )
            max_len = max([len(x) for x in print_list])
            pad_len = [max_len - len(x) for x in print_list]
            # validation summary
            self.rank_zero_logger.info("Metrics:")
            self.rank_zero_logger.info(
                print_prefix
                + "training loss: {}{}".format(get_pad(pad_len[0]), train_logs["loss"])
            )
            self.rank_zero_logger.info(
                print_prefix
                + "validation loss: {}{}".format(
                    get_pad(pad_len[1]), valid_logs["base"]["validation loss"]
                )
            )
            self.rank_zero_logger.info(
                print_prefix
                + "validation L1: {}{}".format(
                    get_pad(pad_len[2]), valid_logs["base"]["validation L1"]
                )
            )
            for idk, key in enumerate(print_list[3:], start=3):
                value = valid_logs["metrics"][key]
                self.rank_zero_logger.info(
                    f"{print_prefix}{key}: {get_pad(pad_len[idk])}{value}"
                )
            self.rank_zero_logger.info(separator)

        if self.params.log_to_wandb:
            wandb.log(train_logs, step=self.epoch)
            wandb.log(valid_logs["base"], step=self.epoch)
            wandb.log(valid_logs["metrics"], step=self.epoch)

        return

    def save_checkpoint(self, checkpoint_path, model=None, checkpoint_mode="flexible"):
        """We intentionally require a checkpoint_dir to be passed
        in order to allow Ray Tune to use this function"""

        if not model:
            model = self.model

        self.rank_zero_logger.info(
            f"Writing checkpoint to {checkpoint_path} ({checkpoint_mode} format)"
        )

        with torch.no_grad():
            # legacy mode
            if checkpoint_mode == "legacy":
                # start timer
                store_start = time.time()
                checkpoint_fname = checkpoint_path.format(
                    mp_rank=comm.get_rank("model")
                )
                store_dict = {
                    "iters": self.iters,
                    "epoch": self.epoch,
                    "model_state": model.state_dict(),
                    "optimizer_state_dict": self.optimizer.state_dict(),
                }
                if self.scheduler is not None:
                    store_dict["scheduler_state_dict"] = self.scheduler.state_dict()
                torch.save(store_dict, checkpoint_fname)

                # stop timer
                store_stop = time.time()

                # report time
                self.rank_zero_logger.info(
                    f"Save checkpoint (legacy): {(store_stop - store_start):.2f} sec ({sys.getsizeof(store_dict)/(1024.**3)}) GB"
                )

            elif checkpoint_mode == "flexible":
                # clear cache
                torch.cuda.empty_cache()

                # start timer
                collect_start = time.time()

                # state_dict = model.state_dict()
                state_dict = OrderedDict()

                for k, v in self.model.named_parameters():
                    weight = v.clone()
                    if hasattr(v, "sharded_dims_mp"):
                        # gather the weight across all sharded dimensions
                        for d, group in enumerate(v.sharded_dims_mp):
                            if group is not None:
                                weight = gather_uneven(weight, d, group)

                    state_dict[k] = weight.to("cpu")

                # stop timer
                collect_stop = time.time()

                # print collect time
                self.rank_zero_logger.info(
                    f"Collect checkpoint (flexible): {(collect_stop - collect_start):.2f} sec."
                )

                # start timer:
                store_start = time.time()

                checkpoint_fname = checkpoint_path.format(mp_rank=0)
                store_dict = {
                    "iters": self.iters,
                    "epoch": self.epoch,
                    "model_state": state_dict,
                    "optimizer_state_dict": self.optimizer.state_dict(),
                }
                if self.scheduler is not None:
                    store_dict["scheduler_state_dict"] = self.scheduler.state_dict()

                # in flexible mode only rank 0 needs to save the data to disk
                if self.world_rank == 0:
                    torch.save(
                        store_dict,
                        checkpoint_fname,
                        _use_new_zipfile_serialization=False,
                    )

                # wait for group
                if dist.is_initialized() and (comm.get_size("model") > 1):
                    dist.barrier(
                        device_ids=[self.device.index], group=comm.get_group("model")
                    )

                # stop timer
                store_stop = time.time()

                self.rank_zero_logger.info(
                    f"Save checkpoint (flexible): {(store_stop - store_start):.2f} sec"
                )
            else:
                raise ValueError(f"Unknown checkoint mode {checkpoint_mode}.")

    def restore_checkpoint(self, checkpoint_path, checkpoint_mode="flexible"):
        """We intentionally require a checkpoint_dir to be passed
        in order to allow Ray Tune to use this function"""
        # legacy mode
        if checkpoint_mode == "legacy":
            checkpoint_fname = checkpoint_path.format(mp_rank=comm.get_rank("model"))

            self.rank_zero_logger.info(f"Loading checkpoint {checkpoint_fname}")

            checkpoint = torch.load(checkpoint_fname, map_location="cpu")

            # this is reworked to avoid loading modules related to the SHT
            state_dict = checkpoint["model_state"]
            self.model.load_state_dict(state_dict, strict=True)

            # If finetuning, restore checkpoint does not load optimizer state, instead uses config specified lr.
            if self.params.resuming:
                self.optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
                if self.scheduler is not None:
                    self.scheduler.load_state_dict(checkpoint["scheduler_state_dict"])
                self.iters = checkpoint["iters"]
                self.startEpoch = checkpoint["epoch"]

        # new flexible mode allows to load models in arbitrary model-parallel configurations
        elif checkpoint_mode == "flexible":
            # when loading the weights in flexble mode we exclusively use mp_rank=0 and load them onto the cpu
            checkpoint_fname = checkpoint_path.format(mp_rank=0)
            self.rank_zero_logger.info(
                f"Loading checkpoint {checkpoint_fname} in flexible mode"
            )
            checkpoint = torch.load(checkpoint_fname, map_location="cpu")

            # this is reworked to avoid loading modules related to the SHT
            state_dict = checkpoint["model_state"]

            with torch.inference_mode():
                with torch.no_grad():
                    for k, v in self.model.named_parameters():
                        if k in state_dict.keys():
                            weight = state_dict[k]

                            if hasattr(v, "sharded_dims_mp"):
                                for d, group in enumerate(v.sharded_dims_mp):
                                    # continue if there is nothing to do
                                    if (group is None) or (comm.get_size(group) == 1):
                                        continue

                                    shard_size = (
                                        weight.shape[d] + comm.get_size(group) - 1
                                    ) // comm.get_size(group)
                                    weight = torch.split(
                                        weight, split_size_or_sections=shard_size, dim=d
                                    )[comm.get_rank(group)]

                            v.copy_(weight)

                        else:
                            # put a warning here
                            print(f"missing {k}")

            # If finetuning, restore checkpoint does not load optimizer state, instead uses config specified lr.
            if self.params.resuming:
                self.iters = checkpoint["iters"]
                self.startEpoch = checkpoint["epoch"]
                # not loading optimzer as momentum tensor shapes might have changed
                # self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
                if self.scheduler is not None:
                    self.scheduler.load_state_dict(checkpoint["scheduler_state_dict"])

        else:
            raise ValueError(f"Unknown checkpoint mode {checkpoint_mode}.")
