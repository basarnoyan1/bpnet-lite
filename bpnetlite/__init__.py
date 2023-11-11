# bpnet-lite
# Author: Jacob Schreiber
# Code adapted from Avanti Shrikumar and Ziga Avsec

from .bpnet import BPNet
from .chrombpnet import ChromBPNet
import wandb

wandb.init(
    project="bpnet-lite-test",
)

__version__ = '0.5.6'
