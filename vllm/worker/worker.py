"""A GPU worker class."""
import os
from typing import Dict, List, Tuple, Optional

import torch
import torch.distributed
from vllm.logger import init_logger
from vllm.config import (CacheConfig, ModelConfig, ParallelConfig,
                         SchedulerConfig)
from vllm.model_executor import get_model, InputMetadata, set_random_seed
from vllm.model_executor.parallel_utils.parallel_state import (
    initialize_model_parallel)
from vllm.sampling_params import SamplingParams
from vllm.sequence import SamplerOutput, SequenceData, SequenceGroupMetadata
from vllm.worker.cache_engine import CacheEngine
from vllm.utils import get_gpu_memory, get_max_shared_memory_bytes
from deepspeed.ops.op_builder import RaggedOpsBuilder
from deepspeed.inference.v2.kernels.ragged_ops import (
    AtomBuilder,
    BlockedFlashAttn,
    get_q_block_size,
    get_kv_block_size,
    LinearBlockedKVCopy,
)
from deepspeed.inference.v2.ragged import (
    AllocationMode,
    DSSequenceDescriptor,
    DSStateManager,
    DSStateManagerConfig,
    KVCacheConfig,
    MemoryConfig,
    PlaceholderSequenceDescriptor,
    RaggedBatchWrapper,
)
from vllm.model_executor.layers import attention

QS_THRSH = 16
logger = init_logger(__name__)

class Worker:
    """A worker class that executes (a partition of) the model on a GPU.

    Each worker is associated with a single GPU. The worker is responsible for
    maintaining the KV cache and executing the model on the GPU. In case of
    distributed inference, each worker is assigned a partition of the model.
    """

    def __init__(
        self,
        model_config: ModelConfig,
        parallel_config: ParallelConfig,
        scheduler_config: SchedulerConfig,
        rank: Optional[int] = None,
        distributed_init_method: Optional[str] = None,
    ) -> None:
        self.model_config = model_config
        self.parallel_config = parallel_config
        self.scheduler_config = scheduler_config
        self.rank = rank
        self.distributed_init_method = distributed_init_method

        # Uninitialized cache engine. Will be initialized by
        # self.init_cache_engine().
        self.cache_config = None
        self.block_size = None
        self.sliding_window = None
        self.cache_engine = None
        self.cache_events = None
        self.gpu_cache = None

        # dont initialize before self.init_model, otherwise will call get cuda device before given a one
        self.kv_block_size = None
        self.q_block_size = None

    def init_model(self):
        # This env var set by Ray causes exceptions with graph building.
        os.environ.pop("NCCL_ASYNC_ERROR_HANDLING", None)
        # Env vars will be set by Ray.
        self.rank = self.rank if self.rank is not None else int(
            os.getenv("RANK", "-1"))
        local_rank = int(os.getenv("LOCAL_RANK", "0"))
        self.device = torch.device(f"cuda:{local_rank}")
        if self.rank < 0:
            raise ValueError("Invalid or unspecified rank.")
        torch.cuda.set_device(self.device)

        # Initialize the distributed environment.
        _init_distributed_environment(self.parallel_config, self.rank,
                                      self.distributed_init_method)

        # Initialize the model.
        set_random_seed(self.model_config.seed)
        self.model = get_model(self.model_config)
        self.kv_block_size = get_kv_block_size(self.model_config.get_head_size())
        self.q_block_size = get_q_block_size(self.model_config.get_head_size())

    @torch.inference_mode()
    def profile_num_available_blocks(
        self,
        block_size: int,
        gpu_memory_utilization: float,
        cpu_swap_space: int,
    ) -> Tuple[int, int]:
        self.prepare_deepspeed_kernel()
        # Profile the memory usage of the model and get the maximum number of
        # cache blocks that can be allocated with the remaining free memory.
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()

        # Profile memory usage with max_num_sequences sequences and the total
        # number of tokens equal to max_num_batched_tokens.

        # Enable top-k sampling to reflect the accurate memory usage.
        vocab_size = self.model.config.vocab_size
        sampling_params = SamplingParams(top_p=0.99, top_k=vocab_size - 1)
        max_num_batched_tokens = self.scheduler_config.max_num_batched_tokens
        max_num_seqs = self.scheduler_config.max_num_seqs
        seqs = []
        for group_id in range(max_num_seqs):
            seq_len = (max_num_batched_tokens // max_num_seqs +
                       (group_id < max_num_batched_tokens % max_num_seqs))
            seq_data = SequenceData([0] * seq_len)
            seq_data.running_inflight_tokens = seq_data.inflight_length
            seq = SequenceGroupMetadata(
                request_id=str(group_id),
                is_prompt=True,
                seq_data={group_id: seq_data},
                sampling_params=sampling_params,
                block_tables=None,
            )
            seqs.append(seq)

        input_tokens, input_positions, input_metadata = self._prepare_inputs(
            seqs)
        # Execute the model.
        num_layers = self.model_config.get_num_layers(self.parallel_config)
        self.model(
            input_ids=input_tokens,
            positions=input_positions,
            kv_caches=[(None, None)] * num_layers,
            input_metadata=input_metadata,
            cache_events=None,
        )

        # Calculate the number of blocks that can be allocated with the
        # profiled peak memory.
        torch.cuda.synchronize()
        peak_memory = torch.cuda.max_memory_allocated()
        total_gpu_memory = get_gpu_memory()
        cache_block_size = CacheEngine.get_cache_block_size(
            block_size, self.model_config, self.parallel_config)
        logger.info(f'cache block size: {cache_block_size} Bytes')
        num_gpu_blocks = int(
            (total_gpu_memory * gpu_memory_utilization - peak_memory) //
            cache_block_size)
        num_cpu_blocks = int(cpu_swap_space // cache_block_size)
        num_gpu_blocks = max(num_gpu_blocks, 0)
        num_cpu_blocks = max(num_cpu_blocks, 0)
        torch.cuda.empty_cache()

        # Reset the seed to ensure that the random state is not affected by
        # the model initialization and profiling.
        set_random_seed(self.model_config.seed)
        return num_gpu_blocks, num_cpu_blocks

    def prepare_deepspeed_kernel(self):
        self.kv_config = KVCacheConfig(block_size=self.kv_block_size,
                                    num_allocation_groups=1,
                                    cache_shape=(self.model_config.get_num_layers(self.parallel_config), 
                                                 self.model_config.get_num_kv_heads(self.parallel_config),
                                                 self.model_config.get_head_size()))
        # dummy allocator setting
        self.memory_config = MemoryConfig(mode=AllocationMode.ALLOCATE, size=1)
        self.ds_manager_config = DSStateManagerConfig(
                                        max_tracked_sequences=2*self.scheduler_config.max_num_seqs,
                                        max_ragged_sequence_count=2*self.scheduler_config.max_num_seqs,
                                        max_ragged_batch_size=2*self.scheduler_config.max_num_batched_tokens,
                                        max_context=self.model_config.max_model_len,
                                        memory_config=self.memory_config)
        self.batch = RaggedBatchWrapper(self.ds_manager_config)
        ids_shape = (
            self.ds_manager_config.max_tracked_sequences,
            self.kv_config.num_allocation_groups,
            self.kv_config.max_blocks_per_allocation_group,
        )
        self.all_block_ids = torch.zeros(ids_shape, dtype=torch.int32, device='cuda')
        self.all_block_ids_shadow = torch.zeros(ids_shape, dtype=torch.int32, device='cpu',pin_memory=True)
        
        self.atom_builder = AtomBuilder()
        max_atoms = self.scheduler_config.max_num_seqs * ((self.model_config.max_model_len + self.q_block_size - 1) // self.q_block_size)
        self.atoms = torch.empty((max_atoms, 8), dtype=torch.int32, device='cuda')
        self.atoms_host = torch.empty((max_atoms, 8), dtype=torch.int32, device='cpu')

    def init_cache_engine(self, cache_config: CacheConfig) -> None:
        self.cache_config = cache_config
        self.block_size = cache_config.block_size
        assert self.block_size == self.kv_block_size, 'they should all get from ds get_kv_block_size()'
        self.sliding_window = cache_config.sliding_window

        if self.sliding_window is None:
            max_seq_len = self.scheduler_config.max_model_len
        else:
            max_seq_len = min(self.scheduler_config.max_model_len,
                              self.sliding_window)
        _check_if_can_support_max_seq_len(max_seq_len, self.block_size)

        self.cache_engine = CacheEngine(self.cache_config, self.model_config,
                                        self.parallel_config)
        self.cache_events = self.cache_engine.events
        self.gpu_cache = self.cache_engine.gpu_cache

    #NOTE(zijian): metadata is managed in sequence granularity
    def _prepare_inputs(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
    ) -> Tuple[torch.Tensor, torch.Tensor, InputMetadata]:
        if not seq_group_metadata_list:
            return None, None, None
        seq_groups: List[Tuple[List[int], SamplingParams]] = []
        input_tokens: List[int] = []
        input_positions: List[int] = []
        slot_mapping: List[int] = []
        is_generating_new_token: List[bool] = []

        # Add prompt tokens.
        # TODO: fix n > 1 case
        prompt_lens: List[int] = []
        for seq_group_metadata in seq_group_metadata_list:
            if not seq_group_metadata.is_prompt or seq_group_metadata.only_swap:
                continue

            seq_ids = list(seq_group_metadata.seq_data.keys())
            sampling_params = seq_group_metadata.sampling_params
            seq_groups.append((seq_ids, sampling_params))

            # Use any sequence in the group.
            seq_id = seq_ids[0]

            sdata = seq_group_metadata.seq_data[seq_id]
            prompt_len = sdata.running_inflight_tokens
            prompt_tokens = sdata.get_token_ids()[:prompt_len]
            prompt_lens.append(prompt_len)
            is_generating_new_token.append(sdata.is_generating())

            input_tokens.extend(prompt_tokens)
            # NOTE(woosuk): Here we assume that the first token in the prompt
            # is always the first token in the sequence.
            input_positions.extend(range(len(prompt_tokens)))

            if seq_group_metadata.block_tables is None:
                # During memory profiling, the block tables are not initialized
                # yet. In this case, we just use a dummy slot mapping.
                slot_mapping.extend([0] * prompt_len)
                continue

            # Compute the slot mapping.
            block_table = seq_group_metadata.block_tables[seq_id]
            for i in range(prompt_len):
                block_number = block_table[i // self.block_size]
                block_offset = i % self.block_size
                slot = block_number * self.block_size + block_offset
                slot_mapping.append(slot)

        # Add generation tokens.
        max_context_len = 0
        max_num_blocks_per_seq = 0
        context_lens: List[int] = []
        generation_block_tables: List[List[int]] = []
        
        # since we mix paused with running, this len help to
        # 1. partition the input token
        # 2. prune the hidden state when sampling the result,
        #    i.e. , we only need the att of the last token in each seq
        self.batch.clear()
        running_query_lens: List[int] = []
        
        for seq_group_metadata in seq_group_metadata_list:
            if seq_group_metadata.is_prompt or seq_group_metadata.only_swap:
                continue
            
            seq_ids = list(seq_group_metadata.seq_data.keys())
            sampling_params = seq_group_metadata.sampling_params
            seq_groups.append((seq_ids, sampling_params))

            for seq_id in seq_ids:
                sdata = seq_group_metadata.seq_data[seq_id]
                qs_to_forward = sdata.running_inflight_tokens
                # logger.info(f'qs_to_forward: {qs_to_forward}')
                start_idx = sdata.get_len() - sdata.inflight_length
                input_tokens.extend(sdata.get_token_ids()[start_idx:start_idx+qs_to_forward])
                running_query_lens.append(qs_to_forward)
                is_generating_new_token.append(sdata.is_generating())
                
                last_token_ctx_len = start_idx + qs_to_forward
                # NOTE: when creating the context length for API-tokens
                # remember to mask future tokens,
                # i.e. only attend to preceding tokens
                hists = [i + last_token_ctx_len - qs_to_forward + 1 \
                    for i in range(qs_to_forward)]
                positions = [h - 1 for h in hists]
                if self.sliding_window is not None:
                    hists = [min(l, self.sliding_window) for l in hists]
                context_lens.append(last_token_ctx_len)
                input_positions.extend(positions)

                block_table = seq_group_metadata.block_tables[seq_id]

                max_context_len = max(max_context_len, hists[-1])
                max_num_blocks_per_seq = max(max_num_blocks_per_seq,
                                             len(block_table))

                for position in positions:
                    block_number = block_table[position // self.block_size]
                    block_offset = position % self.block_size
                    slot = block_number * self.block_size + block_offset
                    slot_mapping.append(slot)
                        
                assert self.sliding_window is None, "sliding window not considered yet"
                if self.sliding_window is not None:
                    sliding_window_blocks = (self.sliding_window //
                                             self.block_size)
                    generation_block_tables.append(block_table[-sliding_window_blocks:])
                else:
                    generation_block_tables.append(block_table)
        if not input_tokens:
            return None, None, None
        # Optimization: Pad the input length to be a multiple of 8.
        # This is required for utilizing the Tensor Cores in NVIDIA GPUs.
        input_tokens = _pad_to_alignment(input_tokens, multiple_of=8)
        input_positions = _pad_to_alignment(input_positions, multiple_of=8)
        
        # Convert to tensors.
        tokens_tensor = torch.tensor(input_tokens,
                                     dtype=torch.long,
                                     device="cuda")
        positions_tensor = torch.tensor(input_positions,
                                        dtype=torch.long,
                                        device="cuda")
        slot_mapping_tensor = torch.tensor(slot_mapping,
                                           dtype=torch.int,
                                           device="cuda")
        context_lens_tensor = torch.tensor(context_lens,
                                           dtype=torch.int,
                                           device="cuda")
        padded_block_tables = [
            _pad_to_max(block_table, max_num_blocks_per_seq)
            for block_table in generation_block_tables
        ]
        block_tables_tensor = torch.tensor(padded_block_tables,
                                           dtype=torch.int32,
                                           device="cuda")

        seq_data: Dict[int, SequenceData] = {}
        for seq_group_metadata in seq_group_metadata_list:
            if seq_group_metadata.only_swap:
                continue
            for k in seq_group_metadata.seq_data.keys():
                assert k not in seq_data, f'{k} already in seq_data'
            seq_data.update(seq_group_metadata.seq_data)

        # prepare ds kernel metadata
        n_atoms = 0
        if running_query_lens:
            for i, (qs, ctx, kv_ids) in enumerate(zip(running_query_lens, context_lens, generation_block_tables)):
                seq_desc = DSSequenceDescriptor(i, 
                                                self.all_block_ids[i], 
                                                self.all_block_ids_shadow[i])
                seq_desc._in_flight_tokens = qs
                seq_desc._seen_tokens = ctx - qs
                seq_desc.extend_kv_cache(torch.tensor(kv_ids, dtype=torch.int32, device='cpu'))
                tokens = torch.zeros(qs, dtype=torch.int32, device='cpu')
                self.batch.insert_sequence(seq_desc, tokens)
            self.batch.finalize()
            atoms_shadow, n_atoms = self.atom_builder(self.atoms_host, self.batch, self.q_block_size, self.kv_block_size)
            self.atoms[:n_atoms].copy_(atoms_shadow[:n_atoms], non_blocking=True)
                
        assert len(is_generating_new_token) == len(seq_groups), f'assume n = 1 for now, {len(is_generating_new_token)}, {len(seq_groups)}'
        input_metadata = InputMetadata(
            seq_groups=seq_groups,
            seq_data=seq_data,
            prompt_lens=prompt_lens,
            slot_mapping=slot_mapping_tensor,
            context_lens=context_lens_tensor,
            max_context_len=max_context_len,
            block_tables=block_tables_tensor,
            running_query_lens=running_query_lens,
            atoms=self.atoms[:n_atoms],
            is_generating_new_token=is_generating_new_token,
            sliding_window=self.sliding_window,
        )
        # logger.info(f'input_metadata: {input_metadata}')
        return tokens_tensor, positions_tensor, input_metadata
    
    @torch.inference_mode()
    def execute_model(
        self,
        seq_group_metadata_list: List[SequenceGroupMetadata],
        blocks_to_swap_in: Dict[int, int],
        blocks_to_swap_out: Dict[int, int],
        blocks_to_copy: Dict[int, List[int]],
    ) -> SamplerOutput:
        attention.sin, attention.sout = len(blocks_to_swap_in), len(blocks_to_swap_out)
        # Issue cache operations.
        issued_cache_op = False
        overlap_blocks_swap_in = blocks_to_swap_in
        overlap_blocks_swap_out = blocks_to_swap_out
        # if self.scheduler_config.api_policy == "S" or self.scheduler_config.api_policy.startswith('H'):
        #     if blocks_to_swap_out:
        #         self.cache_engine.swap_out(blocks_to_swap_out)
        #         issued_cache_op = True
        #     if blocks_to_swap_in:
        #         self.cache_engine.swap_in(blocks_to_swap_in)
        #         issued_cache_op = True
        #     overlap_blocks_swap_in, overlap_blocks_swap_out = {}, {}
        if blocks_to_copy:
            raise ValueError("copy not supported yet")
            self.cache_engine.copy(blocks_to_copy)
            issued_cache_op = True

        if issued_cache_op:
            cache_events = self.cache_events
        else:
            cache_events = None

        # Prepare input tensors.
        torch.cuda.nvtx.range_push("prepare input")
        input_tokens, input_positions, input_metadata = self._prepare_inputs(
            seq_group_metadata_list)
        torch.cuda.nvtx.range_pop()

        # If there is no input, we don't need to execute the model.
        if not input_metadata:
            if blocks_to_swap_out:
                self.cache_engine.swap_out(blocks_to_swap_out)
                issued_cache_op = True
            if blocks_to_swap_in:
                self.cache_engine.swap_in(blocks_to_swap_in)
                issued_cache_op = True
            if issued_cache_op:
                cache_events = self.cache_events
            if cache_events is not None:
                for event in cache_events:
                    event.wait()
            return {}
        
        # Execute the model.
        output = self.model(
            input_ids=input_tokens,
            positions=input_positions,
            kv_caches=self.gpu_cache,
            input_metadata=input_metadata,
            cache_events=cache_events,
            cache_engine=self.cache_engine,
            blocks_to_swap_in=overlap_blocks_swap_in,
            blocks_to_swap_out=overlap_blocks_swap_out,
        )
        return output


def _init_distributed_environment(
    parallel_config: ParallelConfig,
    rank: int,
    distributed_init_method: Optional[str] = None,
) -> None:
    """Initialize the distributed environment."""
    if torch.distributed.is_initialized():
        torch_world_size = torch.distributed.get_world_size()
        if torch_world_size != parallel_config.world_size:
            raise RuntimeError(
                "torch.distributed is already initialized but the torch world "
                "size does not match parallel_config.world_size "
                f"({torch_world_size} vs. {parallel_config.world_size}).")
    elif not distributed_init_method:
        raise ValueError(
            "distributed_init_method must be set if torch.distributed "
            "is not already initialized")
    else:
        torch.distributed.init_process_group(
            backend="nccl",
            world_size=parallel_config.world_size,
            rank=rank,
            init_method=distributed_init_method,
        )

    # A small all_reduce for warmup.
    torch.distributed.all_reduce(torch.zeros(1).cuda())
    initialize_model_parallel(parallel_config.tensor_parallel_size,
                              parallel_config.pipeline_parallel_size)


def _pad_to_alignment(x: List[int], multiple_of: int) -> List[int]:
    return x + [0] * ((-len(x)) % multiple_of)


def _pad_to_max(x: List[int], max_len: int) -> List[int]:
    return x + [0] * (max_len - len(x))


def _check_if_can_support_max_seq_len(max_seq_len: int,
                                      block_size: int) -> None:
    # Follows the logic in
    # attention_kernels.cu::single_query_cached_kv_attention_launcher
    max_shared_mem = get_max_shared_memory_bytes()
    float32_bytes = torch.finfo(torch.float).bits // 8
    padded_max_seq_len = (
        (max_seq_len + block_size - 1) / block_size) * block_size
    # padded_max_seq_len + extra buffer
    required_shared_mem = (padded_max_seq_len + 512) * float32_bytes
    if padded_max_seq_len * float32_bytes > max_shared_mem:
        raise RuntimeError(
            f"vLLM cannot currently support max_model_len={max_seq_len} "
            f"with block_size={block_size} on GPU with compute "
            f"capability {torch.cuda.get_device_capability()} "
            f"(required shared memory {required_shared_mem} > "
            f"available shared memory {max_shared_mem}). "
            "This will be fixed in a future release.")
