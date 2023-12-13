# bpnet-lite
# Author: Jacob Schreiber
# Code adapted from Avanti Shrikumar and Ziga Avsec

from .bpnet import BPNet
import wandb

wandb.init(
    project="bpnet-lite-test",
)

__version__ = '0.7.0'
