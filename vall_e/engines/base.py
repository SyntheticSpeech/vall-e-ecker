from torch import Tensor
from typing import Any, Protocol

Stats = dict[str, float]

class TrainFeeder(Protocol):
	def __call__(
		self, *, engine: "Engine", batch: Any
	) -> None | tuple[Tensor, Stats]:
		...

def default_feeder(engine, batch):
	if isinstance(batch, list):
		engine( *batch )
	elif isinstance(batch, dict):
		engine( **batch )
	else:
		engine( batch )

	losses = engine.gather_attribute("loss")
	loss = torch.stack([*losses.values()]).sum()

	stats = {}
	stats |= {k: v.item() for k, v in losses.items()}

	return loss, stats
	

from ..config import cfg
from ..utils import dispatch_attribute, flatten_dict, gather_attribute, do_gc, to_device
from ..utils.distributed import init_distributed, distributed_initialized, is_global_leader, world_size

import logging
import time
import torch
import torch.distributed
import os

from torch import Tensor
from torch.distributed import all_reduce
from typing import Any, Protocol
from functools import cached_property

from .base import TrainFeeder

_logger = logging.getLogger(__name__)

if not distributed_initialized() and cfg.trainer.backend == "local" and world_size() > 1:
	init_distributed(torch.distributed.init_process_group)

# A very naive engine implementation using barebones PyTorch
class Engine():
	def __init__(self, *args, **kwargs):
		if '_cfg' in kwargs:
			self._cfg = kwargs['_cfg']
			kwargs.pop("_cfg")

		self.module = kwargs['model'].to(cfg.device).to(torch.float32 if cfg.trainer.amp else cfg.trainer.dtype)
		self.optimizer = kwargs['optimizer'] if 'optimizer' in kwargs else None
		self.lr_scheduler = kwargs['lr_scheduler'] if 'lr_scheduler' in kwargs else None

		self.global_steps = kwargs.pop("global_steps", 0)
		self.micro_steps = kwargs.pop("micro_steps", 0)
		self.global_samples = kwargs.pop("global_samples", 0)
		self.tokens_processed = kwargs.pop("tokens_processed", 0)

		self._frozen_params = set()

	def freeze(self, freeze_all=True):
		# set to freeze 
		if self._cfg is None or not hasattr(self._cfg, "frozen_params"):
			raise Exception("freeze_all=False yet self._cfg.frozen_params is None")

		for name, param in self.module.named_parameters():
			if (freeze_all and param.requires_grad) or (not freeze_all and name in self._cfg.frozen_params):
				param.requires_grad_(False)
				self._frozen_params.add(param)

	def unfreeze(self):
		for p in self._frozen_params:
			p.requires_grad_(True)
		self._frozen_params.clear()

	@property
	def _training(self):
		if not hasattr(self, "_cfg"):
			return True
		return self._cfg.training

	@property
	def global_step(self):
		return self.global_steps

	@property
	def micro_step(self):
		return self.micro_steps

	@property
	def batch_size(self):
		return cfg.hyperparameters.batch_size

	@property
	def gradient_accumulation_steps(self):
		return cfg.hyperparameters.gradient_accumulation_steps

	def gather_attribute(self, *args, **kwargs):
		return gather_attribute(self.module, *args, **kwargs)

	def dispatch_attribute(self, *args, **kwargs):
		return dispatch_attribute(self.module, *args, **kwargs)

	def save_checkpoint(self, save_dir, tag ):
		save_path = save_dir / tag / "state.pth"
		save_path.parent.mkdir(parents=True, exist_ok=True)
		torch.save({
			"module": self.module.state_dict(),
			"optimizer": self.optimizer.state_dict() if self.optimizer is not None else None,
			"lr_scheduler": self.lr_scheduler.state_dict() if self.lr_scheduler is not None else None,
			
			"stats": {		
				"global_step": self.global_step,
				"micro_step": self.micro_step,
				"global_samples": self.global_samples,
				"tokens_processed": self.tokens_processed,
			}
		}, save_path)

		open(save_dir / "latest", 'w').write( tag )

	def load_checkpoint(self, load_dir, tag=None, load_module_strict=True, load_optimizer_states=True, load_lr_scheduler_states=True, load_module_only=False):
		if tag is None:
			tag_path = load_dir / "latest"
			if not tag_path.exists():
				return
			tag = open(tag_path).read()

		load_path = load_dir / tag / "state.pth"
		if not load_path.exists():
			return

		state = torch.load(load_path, map_location=torch.device(cfg.device))
		self.global_steps = state['stats']['global_step'] if 'stats' in state else state['global_step']
		self.micro_steps = state['stats']['micro_step'] if 'stats' in state else state['micro_step']
		self.global_samples = state['stats']['global_samples'] if 'stats' in state else state['global_samples']
		self.tokens_processed = state['stats']['tokens_processed'] if 'stats' in state else state['tokens_processed']
		self.module.load_state_dict(state['module'])

		load_optimizer_states = load_optimizer_states and self.optimizer is not None and 'optimizer' in state
		load_lr_scheduler_states = load_lr_scheduler_states and self.lr_scheduler is not None and 'lr_scheduler' in state
		
		if load_optimizer_states:
			self.optimizer.load_state_dict(state['optimizer'], map_location=torch.device(cfg.device))
		
		if load_lr_scheduler_states:
			self.lr_scheduler.load_state_dict(state['lr_scheduler'], map_location=torch.device(cfg.device))

	def eval(self):
		return self.module.eval()
	
	def train(self):
		return self.module.train()

	def to(self, *args, **kwargs):
		self.module = self.module.to(*args, **kwargs)
		if self.optimizer:
			self.optimizer = self.optimizer.to(*args, **kwargs)

		return self

	def __call__(self, *args, **kwargs):
		return self.forward(*args, **kwargs)

	@cached_property
	def device(self):
		return next(self.module.parameters()).device

	def forward(self, *args, **kwargs):
		return self.module.forward(*args, **kwargs)

	def backward(self, loss):
		return (loss / self.gradient_accumulation_steps).backward()

	def step(self):
		with torch.set_grad_enabled(self.gradient_accumulation_steps > 1):
			self.micro_steps += 1 
			self.global_samples += self.batch_size

			if (self.micro_steps + 1) % max(1, self.gradient_accumulation_steps) == 0:
				self.global_steps += 1 
				self.optimizer.step()
				self.optimizer.zero_grad()

	def get_lr(self):
		lrs = []
		for param_group in self.optimizer.param_groups:
			if 'lr' in param_group:
				lrs.append(param_group['lr'])
		return lrs

	def set_lr(self, lr):
		for param_group in self.optimizer.param_groups:
			if 'lr' in param_group:
				param_group['lr'] = lr

	def get_global_grad_norm(self):
		return 0.0

	def traverse(self, *args, **kwargs):
		with torch.autocast("cuda", dtype=cfg.trainer.dtype, enabled=cfg.trainer.amp):
			self.forward(*args, **kwargs)
			losses = self.gather_attribute("loss")
			loss = torch.stack([*losses.values()]).sum()

		stats = {}
		stats |= {k: v.item() for k, v in losses.items()}
		stats |= self.gather_attribute("scalar")

		self.backward(loss)
		self.step()

		return stats

# and now to ignore everything from the above
class Engines(dict[str, Engine]):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.setup()

	def setup(self):
		self._global_step = 0
		self._micro_step = 0
		self._batch_size = 0
		self._global_samples = 0

	@property
	def global_step(self):
		return self._global_step
	
	@property
	def micro_step(self):
		return self._micro_step

	@property
	def batch_size(self):
		return self._batch_size

	@property
	def global_samples(self):
		return self._global_samples

	def gather_attribute(self, *args, **kwargs):
		ret = {}
		for engine in self.values():
			ret |= engine.gather_attribute(*args, **kwargs)
		return ret

	def dispatch_attribute(self, *args, **kwargs):
		for engine in self.values():
			engine.dispatch_attribute(*args, **kwargs)

	def export(self, userdata={}):
		for name, engine in self.items():
			outpath = cfg.ckpt_dir / name / "fp32.pth"
			state_dict = {
				'module': engine.module.state_dict(),
				"stats": {
					"global_step": engine.global_step,
					"micro_step": engine.micro_step,
					"global_samples": engine.global_samples,
					"tokens_processed": engine.tokens_processed,
				},
				"userdata": userdata
			}
			torch.save(state_dict, outpath)
			print(f"Exported {name} to {outpath}")

	def save_checkpoint(self, tag=None):
		if not tag:
			tag = cfg.trainer.save_tag
		tag = tag.lower()
		if tag[:2] == "it" or tag[:4] == "step":
			tag = f'{self.global_step}'

		cfg.ckpt_dir.mkdir(parents=True, exist_ok=True)
		for name, engine in self.items():
			if not engine._training:
				continue

			save_dir = cfg.ckpt_dir / name
			try:
				engine.save_checkpoint(save_dir, tag=tag)
			except Exception as e:
				print(f'Failed to save checkpoint for engine {name}:', str(e))

			# might be better to prune before saving for safety, but [:0] returns an empty list, but I could do [:-cfg.trainer.keep_last_checkpoints - 1 if cfg.trainer.keep_last_checkpoints > 1 else None]
			if cfg.trainer.keep_last_checkpoints > 0 and is_global_leader():
				checkpoints = [ d for d in list(save_dir.glob("*")) if d.is_dir() ]
				checkpoints.sort(key=lambda x: x.stat().st_mtime)
				checkpoints = checkpoints[:-cfg.trainer.keep_last_checkpoints]
				for d in checkpoints:
					if not d.is_dir() or not d.exists():									
						continue
					print("Removing", d)
					for p in d.iterdir():
						p.unlink()
					d.rmdir()

	def load_checkpoint(self, tag=None):
		if not tag:
			tag = cfg.trainer.load_tag

		for name, engine in self.items():
			load_dir = cfg.ckpt_dir / name
			engine.load_checkpoint(
				tag=tag,
				load_dir=load_dir,
				load_module_strict=cfg.trainer.strict_loading,
				load_optimizer_states=False if cfg.trainer.load_module_only else cfg.trainer.load_states,
				load_lr_scheduler_states=False if cfg.trainer.load_module_only else cfg.trainer.load_states,
				load_module_only=cfg.trainer.load_module_only,
			)
			if cfg.trainer.restart_step_count:
				engine.global_steps = 0
				engine.mocro_step = 0
				engine.global_samples = 0
				engine.tokens_processed = 0

		# update the LR because for some god awful reason it gets overwritten when loading from a checkpoint but only when it's not using a scheduler
		if cfg.hyperparameters.scheduler_type == "":
			self.set_lr(cfg.hyperparameters.learning_rate)

		self._update()

	def set_lr(self, lr):
		for engine in self.values():
			if not engine._training:
				continue
			engine.set_lr(lr)

	def _update(self):
		for engine in self.values():
			self._global_step = max(self._global_step, engine.global_step)
			self._micro_step = max(self._micro_step, engine.micro_step)
			self._batch_size = max(self._batch_size, engine.batch_size)
			self._global_samples = max(self._global_samples, engine.global_samples)

	def eval(self):
		for engine in self.values():
			engine.eval()

	def train(self):
		for engine in self.values():
			engine.train()

	def traverse(self):
		stats = {}
		for name, engine in self.items():
			stat = engine.traverse()
			stats.update(flatten_dict({ name.split("-")[0]: stat }))
		return stats

	def step(self, batch, feeder: TrainFeeder = default_feeder):
		total_elapsed_time = 0

		stats: Any = dict()

		if cfg.trainer.gc_mode == 'step':
			do_gc()

		for name, engine in self.items():
			if not engine._training:
				continue

			device = engine.device

			if cfg.trainer.gc_mode == 'substep':
				do_gc()

			start_time = time.time()

			tries = 4
			n_ooms = torch.zeros([], device=device)			
			
			batch = to_device(batch, device)

			if not cfg.trainer.check_for_oom:
				res = feeder( engine=engine, batch=batch )
			else:
				while tries >= 0:
					try:
						res = feeder( engine=engine, batch=batch )
						break
					except RuntimeError as e:
						print("Forward", str(e))

						if "out of memory" not in str(e):
							self.save_checkpoint()
							raise e

						# shrink batch size until it's happy
						for k in batch:
							batch[k] = batch[k][:-1]

						if tries <= 0:
							# trigger OOM
							n_ooms += 1
						else:
							# also do GC
							do_gc()
						continue

				if world_size() > 1:
					all_reduce(n_ooms)
				if n_ooms.item() > 0:
					self.save_checkpoint()
					raise RuntimeError("Out of memory during forward pass!")

			if res is None:
				continue
			
			loss, engine_stats = res
			engine_stats |= self.gather_attribute("scalar")

			n_ooms = torch.zeros([], device=device)
			
			if cfg.trainer.aggressive_optimizations:
				batch = to_device(batch, 'cpu')

			if not cfg.trainer.check_for_oom:
				engine.backward(loss)
			else:
				try:
					engine.backward(loss)
				except RuntimeError as e:
					print("Backwards:", str(e))

					if "out of memory" not in str(e):
						self.save_checkpoint()
						raise e
					
					n_ooms += 1

				if world_size() > 1:
					all_reduce(n_ooms)
				if n_ooms.item() > 0:
					self.save_checkpoint()
					raise RuntimeError("Out of memory during backwards pass!")

			engine.step()
			
			#torch.cuda.synchronize()

			elapsed_time = time.time() - start_time
			total_elapsed_time += elapsed_time

			stats.update(
				flatten_dict(
					{
						name.split("-")[0]: dict(
							loss=loss.item(),
							lr=engine.get_lr()[0],
							grad_norm=engine.get_global_grad_norm(), # This norm is delayed but global and avoids extra computation
							elapsed_time=elapsed_time,
							engine_step=engine.global_step,
							samples_processed=engine.global_samples,
							tokens_processed=engine.tokens_processed,
							**engine_stats,
						)
					}
				),
			)

		self._update()

		if len(self.keys()) > 1:
			stats["elapsed_time"] = total_elapsed_time
		
		stats["it"] = self.global_step

		return stats
