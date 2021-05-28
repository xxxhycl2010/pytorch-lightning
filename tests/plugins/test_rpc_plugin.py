import os
from typing import Optional
from unittest import mock

import pytest

from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import Callback
from pytorch_lightning.plugins.training_type.rpc_sequential import RPCPlugin
from tests.helpers.boring_model import BoringModel
from tests.helpers.runif import RunIf


@mock.patch.dict(
    os.environ,
    {
        "CUDA_VISIBLE_DEVICES": "0,1",
        "SLURM_NTASKS": "2",
        "SLURM_JOB_NAME": "SOME_NAME",
        "SLURM_NODEID": "0",
        "LOCAL_RANK": "0",
        "SLURM_PROCID": "0",
        "SLURM_LOCALID": "0",
    },
)
@mock.patch("torch.cuda.device_count", return_value=2)
@pytest.mark.parametrize(
    ["ddp_backend", "gpus", "num_processes"],
    [("ddp_cpu", None, 2), ("ddp", 2, 0), ("ddp_spawn", 2, 0)],
)
@RunIf(rpc=True)
def test_rpc_choice(tmpdir, ddp_backend, gpus, num_processes):

    class CB(Callback):

        def on_fit_start(self, trainer, pl_module):
            assert isinstance(trainer.training_type_plugin, RPCPlugin)
            raise RuntimeError('finished plugin check')

    model = BoringModel()
    trainer = Trainer(
        default_root_dir=str(tmpdir),
        fast_dev_run=True,
        gpus=gpus,
        num_processes=num_processes,
        distributed_backend=ddp_backend,
        callbacks=[CB()],
        plugins=[RPCPlugin()]
    )

    with pytest.raises(RuntimeError, match='finished plugin check'):
        trainer.fit(model)


class CustomRPCPlugin(RPCPlugin):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.rpc_save_model_count = 0
        self.worker_optimizer_step_count = 0

    def rpc_save_model(self, *_) -> None:
        self.rpc_save_model_count += 1

    def barrier(self, name: Optional[str] = None) -> None:
        return


@RunIf(min_gpus=2, special=True, rpc=True)
def test_rpc_function_calls_ddp(tmpdir):
    model = BoringModel()
    plugin = CustomRPCPlugin()
    max_epochs = 2
    limit_train_batches = 2
    trainer = Trainer(
        limit_train_batches=limit_train_batches,
        limit_val_batches=2,
        max_epochs=max_epochs,
        gpus=2,
        distributed_backend='ddp',
        plugins=[plugin],
        default_root_dir=tmpdir,
    )

    trainer.fit(model)
    if trainer.global_rank == 0:  # Main process
        assert plugin.rpc_save_model_count == max_epochs
    else:  # Worker process
        assert plugin.rpc_save_model_count == max_epochs
