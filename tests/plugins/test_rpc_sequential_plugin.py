# Copyright The PyTorch Lightning team.
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
from unittest import mock

import pytest
import torch
import torch.distributed as torch_distrib
from torch import nn

from pytorch_lightning import LightningModule, Trainer
from pytorch_lightning.plugins.training_type.rpc_sequential import RPCSequentialPlugin
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from tests.helpers.boring_model import RandomDataset
from tests.helpers.runif import RunIf


@mock.patch.dict(os.environ, {"PL_DEV_DEBUG": "1"})
@RunIf(min_gpus=2, special=True, fairscale_pipe=True)
def test_rpc_sequential_plugin_manual(tmpdir):
    model = SequentialModelRPCManual()
    trainer = Trainer(
        max_epochs=2,
        limit_train_batches=2,
        limit_val_batches=2,
        limit_test_batches=2,
        gpus=2,
        distributed_backend="ddp",
        plugins=[RPCSequentialPlugin(balance=[2, 1], rpc_timeout_sec=5 * 60)],
    )

    trainer.fit(model)

    if torch_distrib.is_initialized() and torch_distrib.get_rank() == 0:
        assert len(trainer.dev_debugger.pbar_added_metrics) > 0

    if trainer.accelerator.rpc_enabled:
        # Called at the end of trainer to ensure all processes are killed
        trainer.accelerator.training_type_plugin.exit_rpc_process()


@RunIf(min_gpus=2, special=True, fairscale_pipe=True)
def test_rpc_sequential_plugin_manual_amp(tmpdir):
    model = SequentialModelRPCManual()
    trainer = Trainer(
        max_epochs=2,
        limit_train_batches=2,
        limit_val_batches=2,
        limit_test_batches=2,
        gpus=2,
        precision=16,
        amp_backend="native",
        distributed_backend="ddp",
        plugins=[RPCSequentialPlugin(balance=[2, 1])],
    )
    with pytest.raises(
        MisconfigurationException,
        match='`RPCSequentialPlugin` is currently not supported in Automatic Mixed Precision'
    ):
        trainer.fit(model)


@mock.patch.dict(os.environ, {"PL_DEV_DEBUG": "1"})
@RunIf(min_gpus=2, special=True, fairscale_pipe=True)
def test_rpc_sequential_plugin_automatic(tmpdir):
    model = SequentialModelRPCAutomatic()
    trainer = Trainer(
        max_epochs=2,
        limit_train_batches=2,
        limit_val_batches=2,
        limit_test_batches=2,
        gpus=2,
        distributed_backend="ddp",
        plugins=[RPCSequentialPlugin(balance=[2, 1])],
    )

    trainer.fit(model)

    if torch_distrib.is_initialized() and torch_distrib.get_rank() == 0:
        assert len(trainer.dev_debugger.pbar_added_metrics) > 0

    if trainer.accelerator.rpc_enabled:
        # Called at the end of trainer to ensure all processes are killed
        trainer.accelerator.training_type_plugin.exit_rpc_process()


@RunIf(min_gpus=2, special=True, fairscale_pipe=True)
def test_rpc_sequential_plugin_with_wrong_balance(tmpdir):
    model = SequentialModelRPCAutomatic()
    trainer = Trainer(
        max_epochs=2,
        limit_train_batches=2,
        limit_val_batches=2,
        limit_test_batches=2,
        gpus=2,
        distributed_backend="ddp",
        plugins=[RPCSequentialPlugin(balance=[2, 2])],
    )

    with pytest.raises(
        MisconfigurationException, match="The provided balance sum: 4 does not match your Sequential length: 3"
    ):
        trainer.fit(model)

    if trainer.accelerator.rpc_enabled:
        # Called at the end of trainer to ensure all processes are killed
        trainer.accelerator.training_type_plugin.exit_rpc_process()


class SequentialModelRPCManual(LightningModule):

    def __init__(self):
        super().__init__()
        self.sequential_module = nn.Sequential(torch.nn.Linear(32, 32), nn.ReLU(), nn.Linear(32, 2))
        self.automatic_optimization = False

    def forward(self, x):
        return self.sequential_module(x)

    def loss(self, prediction):
        # An arbitrary loss to have a loss that updates the model weights during `Trainer.fit` calls
        return torch.nn.functional.mse_loss(prediction, torch.ones_like(prediction))

    def step(self, x):
        x = self(x)
        out = torch.nn.functional.mse_loss(x, torch.ones_like(x))
        return out

    def training_step(self, batch, batch_idx):
        opt = self.optimizers()
        output = self.sequential_module(batch)
        loss = self.loss(output)
        self.log("train_loss", loss, on_epoch=True, prog_bar=True)
        self.manual_backward(loss, opt)
        assert torch.stack([torch.abs(p.grad).sum() for p in self.parameters()]).sum() > 0
        opt.step()
        opt.zero_grad()
        assert torch.stack([torch.abs(p.grad).sum() for p in self.parameters()]).sum() == 0

    def validation_step(self, batch, batch_idx):
        output = self.sequential_module(batch)
        loss = self.loss(output)
        return loss

    def test_step(self, batch, batch_idx):
        output = self.sequential_module(batch)
        return self.loss(batch, output)

    def configure_optimizers(self):
        optimizer = torch.optim.SGD(self.parameters(), lr=0.1)
        lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1)
        return [optimizer], [lr_scheduler]

    def train_dataloader(self):
        return torch.utils.data.DataLoader(RandomDataset(32, 64))

    def val_dataloader(self):
        return torch.utils.data.DataLoader(RandomDataset(32, 64))

    def test_dataloader(self):
        return torch.utils.data.DataLoader(RandomDataset(32, 64))


class SequentialModelRPCAutomatic(SequentialModelRPCManual):

    def __init__(self):
        super().__init__()
        self.automatic_optimization = True

    def training_step(self, batch, batch_idx):
        output = self.sequential_module(batch)
        loss = self.loss(output)
        self.log("train_loss", loss, on_epoch=True, prog_bar=True)
        return loss
