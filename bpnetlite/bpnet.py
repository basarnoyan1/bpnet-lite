# bpnet.py
# Author: Jacob Schreiber <jmschreiber91@gmail.com>

"""
This module contains a reference implementation of BPNet that can be used
or adapted for your own circumstances. The implementation takes in a
stranded control track and makes predictions for stranded outputs.
"""

import h5py
import time 
import numpy
import torch
import lightning as L


import wandb

from .losses import MNLLLoss, log1pMSELoss
from .performance import pearson_corr, calculate_performance_measures

from torch.utils.data import DataLoader

from .io import PeakGenerator, extract_loci

from tqdm import tqdm

torch.backends.cudnn.benchmark = True
device = torch.device("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")


class BPNet(L.LightningModule):
	"""A basic BPNet model with stranded profile and total count prediction.

	This is a reference implementation for BPNet. The model takes in
	one-hot encoded sequence, runs it through: 

	(1) a single wide convolution operation 

	THEN 

	(2) a user-defined number of dilated residual convolutions

	THEN

	(3a) profile predictions done using a very wide convolution layer 
	that also takes in stranded control tracks 

	AND

	(3b) total count prediction done using an average pooling on the output
	from 2 followed by concatenation with the log1p of the sum of the
	stranded control tracks and then run through a dense layer.

	This implementation differs from the original BPNet implementation in
	two ways:

	(1) The model concatenates stranded control tracks for profile
	prediction as opposed to adding the two strands together and also then
	smoothing that track 

	(2) The control input for the count prediction task is the log1p of
	the strand-wise sum of the control tracks, as opposed to the raw
	counts themselves.

	(3) A single log softmax is applied across both strands such that
	the logsumexp of both strands together is 0. Put another way, the
	two strands are concatenated together, a log softmax is applied,
	and the MNLL loss is calculated on the concatenation. 

	(4) The count prediction task is predicting the total counts across
	both strands. The counts are then distributed across strands according
	to the single log softmax from 3.

	Note that this model is also used as components in the ChromBPNet model,
	as both the bias model and the accessibility model. Both components are
	the same BPNet architecture but trained on different loci.


	Parameters
	----------
	n_filters: int, optional
		The number of filters to use per convolution. Default is 64.

	n_layers: int, optional
		The number of dilated residual layers to include in the model.
		Default is 8.

	n_outputs: int, optional
		The number of profile outputs from the model. Generally either 1 or 2 
		depending on if the data is unstranded or stranded. Default is 2.

	n_control_tracks: int, optional
		The number of control tracks to feed into the model. When predicting
		TFs, this is usually 2. When predicting accessibility, this is usualy
		0. When 0, this input is removed from the model. Default is 2.

	alpha: float, optional
		The weight to put on the count loss.

	profile_output_bias: bool, optional
		Whether to include a bias term in the final profile convolution.
		Removing this term can help with attribution stability and will usually
		not affect performance. Default is True.

	count_output_bias: bool, optional
		Whether to include a bias term in the linear layer used to predict
		counts. Removing this term can help with attribution stability but
		may affect performance. Default is True.

	name: str or None, optional
		The name to save the model to during training.

	trimming: int or None, optional
		The amount to trim from both sides of the input window to get the
		output window. This value is removed from both sides, so the total
		number of positions removed is 2*trimming.

	verbose: bool, optional
		Whether to display statistics during training. Setting this to False
		will still save the file at the end, but does not print anything to
		screen during training. Default is True.
	"""

	def __init__(self, n_filters=64, n_layers=8, n_outputs=2, 
		n_control_tracks=2, alpha=1, profile_output_bias=True, 
		count_output_bias=True, name=None, trimming=None, verbose=True, 
  		lr=0.001, batch_size=64, train_params=None, val_params=None,
    	X_valid=None, X_ctl_valid=None, y_valid=None):
		super(BPNet, self).__init__()
  
		wandb.config.device = device
  
		self.n_filters = n_filters
		self.n_layers = n_layers
		self.n_outputs = n_outputs
		self.n_control_tracks = n_control_tracks
		self.lr = lr
		self.alpha = alpha
		self.name = name or "bpnet.{}.{}".format(n_filters, n_layers)
		self.trimming = trimming or 2 ** n_layers
  
		self.batch_size = batch_size
  
		self.train_params = train_params
		self.val_params = val_params 
  
		self.X_valid = X_valid
		self.X_ctl_valid = X_ctl_valid
		self.y_valid = y_valid

		wandb.config.n_filters = n_filters
		wandb.config.n_layers = n_layers
		wandb.config.profile_output_bias = profile_output_bias
		wandb.config.count_output_bias = count_output_bias
		wandb.config.name = name
		wandb.config.alpha = alpha
		wandb.config.n_outputs = n_outputs
		wandb.config.n_control_tracks = n_control_tracks

		self.iconv = torch.nn.Conv1d(4, n_filters, kernel_size=21, padding=10)
		self.irelu = torch.nn.ReLU()

		self.rconvs = torch.nn.ModuleList([
			torch.nn.Conv1d(n_filters, n_filters, kernel_size=3, padding=2**i, 
				dilation=2**i) for i in range(1, self.n_layers+1)
		])
		self.rrelus = torch.nn.ModuleList([
			torch.nn.ReLU() for i in range(1, self.n_layers+1)
		])

		self.fconv = torch.nn.Conv1d(n_filters+n_control_tracks, n_outputs, 
			kernel_size=75, padding=37, bias=profile_output_bias)
		
		n_count_control = 1 if n_control_tracks > 0 else 0
		self.linear = torch.nn.Linear(n_filters+n_count_control, 1, 
			bias=count_output_bias)


	def forward(self, X, X_ctl=None):
		"""A forward pass of the model.

		This method takes in a nucleotide sequence X, a corresponding
		per-position value from a control track, and a per-locus value
		from the control track and makes predictions for the profile 
		and for the counts. This per-locus value is usually the
		log(sum(X_ctl_profile)+1) when the control is an experimental
		read track but can also be the output from another model.

		Parameters
		----------
		X: torch.tensor, shape=(batch_size, 4, length)
			The one-hot encoded batch of sequences.

		X_ctl: torch.tensor or None, shape=(batch_size, n_strands, length)
			A value representing the signal of the control at each position in 
			the sequence. If no controls, pass in None. Default is None.

		Returns
		-------
		y_profile: torch.tensor, shape=(batch_size, n_strands, out_length)
			The output predictions for each strand trimmed to the output
			length.
		"""

		start, end = self.trimming, X.shape[2] - self.trimming

		X = self.irelu(self.iconv(X))
		for i in range(self.n_layers):
			X_conv = self.rrelus[i](self.rconvs[i](X))
			X = torch.add(X, X_conv)

		if X_ctl is None:
			X_w_ctl = X
		else:
			X_w_ctl = torch.cat([X, X_ctl], dim=1)

		y_profile = self.fconv(X_w_ctl)[:, :, start:end]

		# counts prediction
		X = torch.mean(X[:, :, start-37:end+37], dim=2)

		if X_ctl is not None:
			X_ctl = torch.sum(X_ctl[:, :, start-37:end+37], dim=(1, 2))
			X_ctl = X_ctl.unsqueeze(-1)
			X = torch.cat([X, torch.log(X_ctl+1)], dim=-1)

		y_counts = self.linear(X).reshape(X.shape[0], 1)
		return y_profile, y_counts

	def predict(self, X, X_ctl=None, batch_size=64, verbose=False):
		"""Make predictions for a large number of examples.

		This method will make predictions for a number of examples that exceed
		the batch size. It is similar to the forward method in terms of inputs 
		and outputs, but will run wrapped with `torch.no_grad()` to speed up
		computation and prevent information leakage into the model.


		Parameters
		----------
		X: torch.tensor, shape=(-1, 4, length)
			The one-hot encoded batch of sequences.

		X_ctl: torch.tensor or None, shape=(-1, n_strands, length)
			A value representing the signal of the control at each position in 
			the sequence. If no controls, pass in None. Default is None.

		batch_size: int, optional
			The number of examples to run at a time. Default is 64.

		verbose: bool
			Whether to print a progress bar during predictions.


		Returns
		-------
		y_profile: torch.tensor, shape=(-1, n_strands, out_length)
			The output predictions for each strand trimmed to the output
			length.
		"""

		starts = numpy.arange(0, X.shape[0], batch_size)
		ends = starts + batch_size

		y_profiles, y_counts = [], []
		for start, end in tqdm(zip(starts, ends), disable=not verbose):
			X_batch = X[start:end]
			X_ctl_batch = None if X_ctl is None else X_ctl[start:end]

			y_profiles_, y_counts_ = self(X_batch, X_ctl_batch)
			
			y_profiles.append(y_profiles_)
			y_counts.append(y_counts_)

		y_profiles = torch.cat(y_profiles)
		y_counts = torch.cat(y_counts)
		return y_profiles, y_counts


	def training_step(self, batch, batch_idx):
		X, y, X_ctl = batch[0], batch[-1], batch[1] if len(batch) == 3 else None
		if self.X_valid is not None:
			y_valid_counts = self.y_valid.sum(dim=2)

		y_profile, y_counts = self(X, X_ctl)
		y_profile = y_profile.reshape(y_profile.shape[0], -1)
		y_profile = torch.nn.functional.log_softmax(y_profile, dim=-1)
		y = y.reshape(y.shape[0], -1)
  
		profile_loss = MNLLLoss(y_profile, y).mean()
		count_loss = log1pMSELoss(y_counts, y.sum(dim=-1).reshape(-1, 1)).mean()
		profile_loss_ = profile_loss.item()
		count_loss_ = count_loss.item()
  
		loss = profile_loss + self.alpha * count_loss
		loss.backward()
		#optimizer.step()
		#loss = self.compute_loss(y_profile, y) #Check
		#self.log('train_loss', loss, on_step=True, on_epoch=True, prog_bar=True, logger=True)
  
		self.eval()

		
		y_profile, y_counts = self.predict(self.X_valid, self.X_ctl_valid)

		z = y_profile.shape
		y_profile = y_profile.reshape(y_profile.shape[0], -1)
		y_profile = torch.nn.functional.log_softmax(y_profile, dim=-1)
		y_profile = y_profile.reshape(*z)

		measures = calculate_performance_measures(y_profile, 
			self.y_valid, y_counts, kernel_sigma=7, 
			kernel_width=81, measures=['profile_mnll', 
			'profile_pearson', 'count_pearson', 'count_mse'])

		profile_corr = measures['profile_pearson']
		count_corr = measures['count_pearson']
		
		valid_loss = measures['profile_mnll'].mean()
		valid_loss += self.alpha * measures['count_mse'].mean()
  
		wandb.log({
		 	"epoch": self.current_epoch + 1, 
		 	"iteration": self.global_step, 	
		 	#"train_time": train_time, 
		 	#"valid_time": valid_time, 
		 	"training_mnll": profile_loss_, 
		 	"training_count_mse": count_loss_,
		 	"valid_mnll": measures['profile_mnll'].mean().item(), 
		 	"valid_profile_pearson": numpy.nan_to_num(profile_corr).mean(),
		 	"valid_count_pearson": numpy.nan_to_num(count_corr).mean(), 
		 	"valid_count_mse": measures['count_mse'].mean().item()
    })


		# if valid_loss < best_loss:
		# 	torch.save(self, "{}.torch".format(self.name))
		# 	best_loss = valid_loss
		# 	early_stop_count = 0
		# else:
		# 	early_stop_count += 1
  
		return loss


	#def compute_loss(self, y_pred, y):

		# profile_loss = MNLLLoss(y_pred, y)
		# count_loss = log1pMSELoss(y_pred, y)
		# return (profile_loss + self.alpha * count_loss).mean()


	def configure_optimizers(self):
		return torch.optim.AdamW(self.parameters(), lr=self.lr)

	def train_dataloader(self):
		return PeakGenerator(**self.train_params)

	# def val_dataloader(self):
	# 	val_data = extract_loci(**self.val_params)
	# 	return val_data



	# @classmethod
	# def from_chrombpnet_lite(cls, filename):
	# 	"""Loads a model from ChromBPNet-lite TensorFlow format.
	
	# 	This method will load a ChromBPNet-lite model from TensorFlow format.
	# 	Note that this is not the same as ChromBPNet format. Specifically,
	# 	ChromBPNet-lite was a preceeding package that had a slightly different
	# 	saving format, whereas ChromBPNet is the packaged version of that
	# 	code that is applied at scale.

	# 	This method does not load the entire ChromBPNet model. If that is
	# 	the desired behavior, see the `ChromBPNet` object and its associated
	# 	loading functions. Instead, this loads a single BPNet model -- either
	# 	the bias model or the accessibility model, depending on what is encoded
	# 	in the stored file.


	# 	Parameters
	# 	----------
	# 	filename: str
	# 		The name of the h5 file that stores the trained model parameters.


	# 	Returns
	# 	-------
	# 	model: BPNet
	# 		A BPNet model compatible with this repository in PyTorch.
	# 	"""

	# 	h5 = h5py.File(filename, "r")
	# 	w = h5['model_weights']

	# 	if 'model_1' in w.keys():
	# 		w = w['model_1']
	# 		bias = False
	# 	else:
	# 		bias = True

	# 	k, b = 'kernel:0', 'bias:0'
	# 	name = "conv1d_{}_1" if not bias else "conv1d_{0}/conv1d_{0}"

	# 	layer_names = []
	# 	for layer_name in w.keys():
	# 		try:
	# 			idx = int(layer_name.split("_")[1])
	# 			layer_names.append(idx)
	# 		except:
	# 			pass

	# 	n_filters = w[name.format(1)][k].shape[2]
	# 	n_layers = max(layer_names) - 2

	# 	model = BPNet(n_layers=n_layers, n_filters=n_filters, n_outputs=1,
	# 		n_control_tracks=0, trimming=(2114-1000)//2)

	# 	convert_w = lambda x: torch.nn.Parameter(torch.tensor(
	# 		x[:]).permute(2, 1, 0))
	# 	convert_b = lambda x: torch.nn.Parameter(torch.tensor(x[:]))

	# 	model.iconv.weight = convert_w(w[name.format(1)][k])
	# 	model.iconv.bias = convert_b(w[name.format(1)][b])
	# 	model.iconv.padding = 12

	# 	for i in range(2, n_layers+2):
	# 		model.rconvs[i-2].weight = convert_w(w[name.format(i)][k])
	# 		model.rconvs[i-2].bias = convert_b(w[name.format(i)][b])

	# 	model.fconv.weight = convert_w(w[name.format(n_layers+2)][k])
	# 	model.fconv.bias = convert_b(w[name.format(n_layers+2)][b])
	# 	model.fconv.padding = 12

	# 	name = "logcounts_1" if not bias else "logcounts/logcounts"
	# 	model.linear.weight = torch.nn.Parameter(torch.tensor(w[name][k][:].T))
	# 	model.linear.bias = convert_b(w[name][b])
	# 	return model

	# @classmethod
	# def from_chrombpnet(cls, filename):
		"""Loads a model from ChromBPNet TensorFlow format.
	
		This method will load one of the components of a ChromBPNet model
		from TensorFlow format. Note that a full ChromBPNet model is made up
		of an accessibility model and a bias model and that this will load
		one of the two. Use `ChromBPNet.from_chrombpnet` to end up with the
		entire ChromBPNet model.


		Parameters
		----------
		filename: str
			The name of the h5 file that stores the trained model parameters.


		Returns
		-------
		model: BPNet
			A BPNet model compatible with this repository in PyTorch.
		"""

		h5 = h5py.File(filename, "r")
		w = h5['model_weights']

		if 'bpnet_1conv' in w.keys():
			prefix = ""
		else:
			prefix = "wo_bias_"

		namer = lambda prefix, suffix: '{0}{1}/{0}{1}'.format(prefix, suffix)
		k, b = 'kernel:0', 'bias:0'

		n_layers = 0
		for layer_name in w.keys():
			try:
				idx = int(layer_name.split("_")[-1].replace("conv", ""))
				n_layers = max(n_layers, idx)
			except:
				pass

		name = namer(prefix, "bpnet_1conv")
		n_filters = w[name][k].shape[2]

		model = BPNet(n_layers=n_layers, n_filters=n_filters, n_outputs=1,
			n_control_tracks=0, trimming=(2114-1000)//2)

		convert_w = lambda x: torch.nn.Parameter(torch.tensor(
			x[:]).permute(2, 1, 0))
		convert_b = lambda x: torch.nn.Parameter(torch.tensor(x[:]))

		iname = namer(prefix, 'bpnet_1st_conv')

		model.iconv.weight = convert_w(w[iname][k])
		model.iconv.bias = convert_b(w[iname][b])
		model.iconv.padding = (21 - 1) // 2

		for i in range(1, n_layers+1):
			lname = namer(prefix, 'bpnet_{}conv'.format(i))

			model.rconvs[i-1].weight = convert_w(w[lname][k])
			model.rconvs[i-1].bias = convert_b(w[lname][b])

		prefix = prefix + "bpnet_" if prefix != "" else ""

		fname = namer(prefix, 'prof_out_precrop')
		model.fconv.weight = convert_w(w[fname][k])
		model.fconv.bias = convert_b(w[fname][b])
		model.fconv.padding = (75 - 1) // 2

		name = namer(prefix, "logcount_predictions")
		model.linear.weight = torch.nn.Parameter(torch.tensor(w[name][k][:].T))
		model.linear.bias = convert_b(w[name][b])
		return model