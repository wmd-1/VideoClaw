# MiniMaxAI/MiniMax-H3 on vLLM

- Source: https://recipes.vllm.ai/MiniMaxAI/MiniMax-H3
- Provider: MiniMax
- Released: 2026-07-28T10:45:18.000Z
- Updated: 2026-09-07
- Architecture: dense
- Parameters: 64B
- Min vLLM: 0.26.0
- Tasks: omni
- Verified hardware: GB200 NVL4, B300, DGX Spark (GB10), RTX 4090, RTX PRO 5000 Blackwell 4x, RTX 5090, RTX Pro 6000 2x, MI300X, MI325X, MI355X, Ascend 950PR
- Summary: Open-weight general-purpose multimodal generation model — jointly generates 24 FPS video with native stereo audio from text, image, video, and audio references, served via vLLM-Omni

> Saved locally from https://recipes.vllm.ai/MiniMaxAI/MiniMax-H3 on 2026-09-29 (raw Markdown source, all variants and in-page anchors included).

## Variants
- default: precision=bf16, vram_minimum_gb=202
  BF16 modular service — two 66.3 GB DiTs share the 51.5 GB Qwen3-VL encoder and ~10.6 GB VAEs. Use --task-type fl2va or ref2va on memory-constrained profiles to load one DiT.

## Recommended base flags
```bash
vllm serve 'MiniMaxAI/MiniMax-H3' \
  --trust-remote-code \
  --host \
  0.0.0.0 \
  --port \
  8000 \
  --num-gpus \
  4 \
  --usp \
  4 \
  --ring \
  1 \
  --vae-patch-parallel-size \
  4 \
  --vae-parallel-mode \
  tile \
  --vae-use-tiling
```

Environment:
```bash
VLLM_WORKER_MULTIPROC_METHOD='spawn'
VLLM_OMNI_VIDEO_SYNC_TIMEOUT='1800'
```

## Guide

## Overview

MiniMax H3 is an **open-weight, general-purpose multimodal generation model**. Rather than
being confined to one specialized task — generate, edit, or reference — H3 reads a
multimodal context that mixes text, images, video, and audio together, interprets the
creative intent as a whole, and produces coherent audio-visual output end to end.

Architecturally it is a **CFG-distilled joint video/audio diffusion transformer** served
through vLLM-Omni's OpenAI-compatible `/v1/videos` API. Every request returns a single MP4
containing H.264 video and **native stereo audio** — the audio is generated jointly with
the video by the same DiT, not dubbed on afterwards.

Three capability areas the model targets:

- **Commercial-grade generation** — film and entertainment, advertising and branding,
  e-commerce, gaming; dynamic typography, VFX, product showcases, UI/UX motion design.
- **Native multimodal understanding + generation** — interprets characters, motion, sound,
  emotion, cinematography, and visual style across mixed references, then combines them.
- **Precise multimodal editing and control** — iterative refinement of characters, objects,
  scenes, sound, and rhythm with strong instruction following.

The checkpoint contains two DiT backbones that share the encoder and VAEs:

| DiT | Tasks | Conditioning |
|---|---|---|
| `FL2VA` | `t2va`, `fl2va` | text only, or text + first/last frame |
| `Ref2VA` | `ref2va` | text + omni references (images, video clips, audio clips) |

Since vLLM-Omni [#5720](https://github.com/vllm-project/vllm-omni/pull/5720), serving
`/path/to/MiniMax-H3` initializes both DiTs by default, while the Qwen3-VL encoder and
video/audio VAEs stay shared. One server can therefore handle all three task types.
Add `--task-type fl2va` or `--task-type ref2va` only when the hardware profile needs to
load one DiT to save GPU or host memory.

Components (BF16): two 52-block joint video/audio DiTs (66.3 GB each), a shared
Qwen3-VL layer-50 text/vision encoder (51.5 GB), a video VAE (~10 GB), and an audio
VAE (~0.6 GB).

## Model specifications

| | |
|---|---|
| Output duration | 4–15 seconds |
| Frame rate | 24 FPS (fixed) |
| Audio | Native stereo, on every generation |
| Resolution — 1440p (2K) | Short edge 1440 px for 16:9 … 9:16; wider formats ≈3.7 MP (e.g. 2976×1248 at 21:9) |
| Resolution — 768p | Short edge 768 px for 16:9 … 9:16; wider formats ≈1 MP (e.g. 1536×672 at 21:9). Upscalable to 1440p. *(mode coming soon)* |
| Aspect ratio | First/Last Frame follows the uploaded image. Text-to-Video and Omni Reference select from 21:9, 16:9, 4:3, 1:1, 3:4, 9:16 — Omni Reference also offers Auto |
| Prompt length | Up to 7,000 characters |

### Input modes and limits

**First/Last Frame** — 0, 1, or 2 images. Edge lengths in `[256, 5760]`, aspect ratio 5:2
to 2:5. With no image, H3 falls back to Text-to-Video.

**Omni Reference** — up to 9 images; up to 3 video clips (2–15 s each, 15 s total); up to
3 audio clips (2–15 s each, 15 s total). Edge lengths in `[256, 5760]`, video aspect ratio
5:2 to 2:5. **Audio must be paired with at least one image or video** — it cannot be the
sole reference. Mixed inputs cap at 12 files total; with none, H3 falls back to
Text-to-Video.

| | Formats | Per-file size |
|---|---|---|
| Video | H.264/AVC, H.265/HEVC (embedded audio AAC or MP3) | 50 MB |
| Image | JPG, JPEG, PNG, WEBP, HEIC, HEIF | 30 MB |
| Audio | WAV, MP3 | 15 MB |

Limits are per file, not per combined upload. Keep the whole request body under 64 MB —
prefer URL-based media over inlined uploads for anything large.

## Prerequisites

H3 can be served directly from the gated HF ID or from a local root directory. The local root contains both DiT
subdirectories plus the shared encoder and VAEs. Request access on the gated model card,
then download the complete repository:

```bash
hf download MiniMaxAI/MiniMax-H3 --local-dir /path/to/MiniMax-H3
```

That yields `/path/to/MiniMax-H3/FL2VA`, `/path/to/MiniMax-H3/Ref2VA`, and the
shared component directories. Serve `/path/to/MiniMax-H3` itself and substitute your own
`--local-dir` throughout the commands below.

`ffmpeg` and `ffprobe` must be on `PATH` — they are used for reference-video preparation
and MP4 muxing.

## Installation

### Docker (recommended)

The image bundles H3 handlers and media dependencies. The published tag predates the
modular pipeline merged in vLLM-Omni #5720, so clone current `main` before starting:

```bash
docker pull vllm/vllm-omni:minimax-h3
git clone --depth 1 https://github.com/vllm-project/vllm-omni.git /path/to/vllm-omni
```

The command builder's Docker mode binds both the root checkpoint and source checkout,
then prepends the checkout to `PYTHONPATH`:

```bash
-v /path/to/MiniMax-H3:/path/to/MiniMax-H3:ro
-v /path/to/vllm-omni:/path/to/vllm-omni:ro
-e PYTHONPATH=/path/to/vllm-omni
```

### pip

H3 support ships in vLLM-Omni rather than the `vllm` wheel, so the pip path needs a
current source checkout:

```bash
uv venv
source .venv/bin/activate
uv pip install vllm==0.26.0
git clone --depth 1 https://github.com/vllm-project/vllm-omni.git /path/to/vllm-omni
cd /path/to/vllm-omni
uv pip install -e .
```

The generated datacenter Blackwell route uses the default `TRTLLM_ATTN`; RTX PRO 5000 and the two-GPU RTX profiles use `CUDNN_ATTN`. These routes need no
FlashAttention-4 install. Install the optional extra only when explicitly comparing FA4:
`uv pip install -e '.[fa4]'`.

## Launch — two 24/32 GB GPUs with distributed layerwise offload

vLLM-Omni includes a low-memory TP2 path for H3. Install the current vLLM-Omni
source checkout as shown above.

The two-GPU profile combines TP2 with distributed layerwise offload (DLO). Each rank
keeps its rank-local TP shard in pinned host memory; `--dlo-no-use-allgather` streams
those shards directly instead of reconstructing full DiT blocks on every rank. A
configurable prefix of DiT blocks stays resident for the complete denoise stage, then
releases its HBM before VAE decode.

Select the profile first. RTX 5090 uses 20 resident DiT layers at the released
1344×768 shape; RTX 4090 keeps more HBM headroom with 12 resident layers and a
1024×576 starting shape:

```bash
# 2× RTX 5090 (32 GB)
export DLO_RESIDENT_LAYERS=20 WIDTH=1344 HEIGHT=768

# Or, 2× RTX 4090 (24 GB)
# export DLO_RESIDENT_LAYERS=12 WIDTH=1024 HEIGHT=576
```

Load only the FL2VA DiT on these memory-constrained profiles:

```bash
CUDA_VISIBLE_DEVICES=0,1 \
VLLM_WORKER_MULTIPROC_METHOD=spawn \
VLLM_OMNI_VIDEO_SYNC_TIMEOUT=14400 \
vllm serve /path/to/MiniMax-H3 \
  --omni \
  --task-type fl2va \
  --host 0.0.0.0 \
  --port 8000 \
  --trust-remote-code \
  --num-gpus 2 \
  --tensor-parallel-size 2 \
  --usp 1 \
  --ring 1 \
  --text-encoder-tp-size 2 \
  --vae-patch-parallel-size 2 \
  --vae-parallel-mode tile \
  --vae-use-tiling \
  --enable-distributed-layerwise-offload \
  --dlo-no-use-allgather \
  --dlo-resident-layers "${DLO_RESIDENT_LAYERS}" \
  --enforce-eager \
  --diffusion-attention-backend CUDNN_ATTN
```

The resident count changes placement and transfer frequency only; it does not quantize
or change the BF16/FP32 denoise math. Use a **384 GiB-class host** for the pinned model
shards and offload buffers.

Wait for `Application startup complete`, then verify health and run one full 50-step
T2VA request:

```bash
curl --fail http://127.0.0.1:8000/health

curl --fail-with-body -sS \
  -X POST http://127.0.0.1:8000/v1/videos/sync \
  -F 'prompt=At night, three cats march into a bedroom playing tiny brass instruments, then abruptly file out, with synchronized room ambience.' \
  -F "width=${WIDTH}" \
  -F "height=${HEIGHT}" \
  -F 'fps=24' \
  -F 'num_inference_steps=50' \
  -F 'flow_shift=12' \
  -F 'seed=1101' \
  -F 'extra_params={"task":"t2va","duration":5.0,"audio_flow_shift":3.0}' \
  -o minimax-h3-t2va.mp4

ffprobe -v error -show_entries \
  stream=index,codec_name,width,height,r_frame_rate,sample_rate,channels \
  -of json minimax-h3-t2va.mp4
ffmpeg -v error -i minimax-h3-t2va.mp4 \
  -map 0:v:0 -map 0:a:0 -f null -
```
**Note:** If using 2× RTX 4090, the curl request must include -F 'aspect_ratio=16:9' to succeed.

A successful output has H.264 video at 24 FPS and stereo AAC audio at 32 kHz, and the
final `ffmpeg` command exits with status 0. This capacity profile intentionally loads one
DiT. Restart the same root-model command with `--task-type ref2va` for Ref2VA; remove
`--task-type` only after confirming that the combined modular service fits the target.

The same root path is used for both task families; only `--task-type` changes on these
capacity profiles.

## Launch — four GPUs (validated best practice)

On B300/GB200, the no-offload root service uses Ulysses SP4, tiled VAE patch
parallelism 4, regional `torch.compile`, dense BF16 `TRTLLM_ATTN`, and Ring/TP1. Both DiTs load
once while the encoder and VAEs remain shared.

```bash
CUDA_VISIBLE_DEVICES=0,1,2,3 \
FLASHINFER_DISABLE_VERSION_CHECK=1 \
VLLM_WORKER_MULTIPROC_METHOD=spawn \
VLLM_OMNI_VIDEO_SYNC_TIMEOUT=1800 \
vllm serve /path/to/MiniMax-H3 \
  --omni \
  --host 0.0.0.0 \
  --port 8000 \
  --trust-remote-code \
  --num-gpus 4 \
  --usp 4 \
  --ring 1 \
  --vae-patch-parallel-size 4 \
  --vae-parallel-mode tile \
  --vae-use-tiling
```

Omitting the attention flag is intentional: current datacenter Blackwell builds default to
dense BF16 `TRTLLM_ATTN`. Install `[fa4]` and add `--diffusion-attention-backend FLASH_ATTN`
only for an explicit FA4 comparison.

### Optional lossy attention: SAGE + Skip-Softmax

`TRTLLM_ATTN` offers two **lossy** optimizations for the long main DiT attention sequence.
Both work under the pure Ulysses parallelism of the four-GPU profile (`--usp 4 --ring 1`) and
are datacenter-Blackwell only. Append the following to the four-GPU command above to enable
both:

```bash
--diffusion-attention-config '{
  "default": {
    "backend": "TRTLLM_ATTN",
    "quant": {"dtype_qk": "fp8_e4m3", "q_block_size": 1, "k_block_size": 16},
    "skip_softmax": {"threshold": 0.05, "disabled_until_timestep": 0.97}
  },
  "per_role": {
    "minimax_h3.token_refiner": {"backend": "TRTLLM_ATTN"}
  }
}'
```

- **SAGE** quantizes Q/K to `fp8_e4m3` for `QK^T`; P and V are always FP8 in this kernel.
  B200 additionally supports `int8` Q/K, which preserves accuracy better than FP8.
- **Skip-Softmax** skips the Softmax and `PV` work of KV tiles whose scores fall far below
  the running row maximum. `threshold=0.05` with `disabled_until_timestep=0.97` is a
  **conservative** setting: a low threshold skips only clearly negligible tiles, and the
  cutoff keeps the early high-noise steps dense — H3's default video flow shift of 12 keeps
  the normalized timestep above `0.97` for the first 14 of 49 denoiser forwards at 50 steps.
  Raise `threshold` or lower `disabled_until_timestep` for more speedup once quality is
  verified.
- The `per_role` entry keeps the short token-refiner attention dense; a per-role spec does
  not inherit `quant` or `skip_softmax` from `default`.

Both optimizations trade fidelity for speed, and their effects compound. The measured B300
speedup and LPIPS for each mode and for the combination are in the vLLM blog's
[Quantized and Sparse Attention](https://vllm.ai/blog/2026-09-01-minimax-h3-production-serving#quantized-and-sparse-attention-in-trtllm_attn)
section as a reference; tune the settings according to the accuracy and speed required. See
the
[TRTLLM attention guide](https://docs.vllm.ai/projects/vllm-omni/en/latest/user_guide/diffusion/attention_backends/trtllm/)
for the full configuration reference.

No restart is required: `extra_params.task` routes T2VA/FL2VA requests to the FL2VA
DiT and Ref2VA requests to the Ref2VA DiT.

### FastH3 four-step Dense and VSA adapters

[FastH3](https://haoailab.com/blogs/fasth3-preview/) is FastVideo's four-step DMD2
student of H3-Base. It keeps H3's encoder, VAEs, tokenizers, and schedulers, but replaces
49 denoiser evaluations with four. Preview v1 is T2VA-only and is fused into the FL2VA
DiT at server startup, so run it in a dedicated process rather than expecting request-time
LoRA switching.

This path requires current vLLM-Omni `main` at or after the merge of #6909. The PR was
validated with vLLM 0.28.0; upgrade the core package to that version when reproducing its
environment, while the base H3 paths elsewhere in this recipe retain their v0.26.0 floor.

The release contains one dense and three VSA artifacts. The commands below compare the
Dense / Data-Free baseline with the recommended VSA / Data-Free artifact. Download both
under the H3 root so the existing Docker bind mount includes them:

```bash
hf download FastVideo/FastVideo-FastH3-4-step-Preview-v1-LoRA \
  dense-datafree/adapter_model.safetensors \
  vsa-datafree/adapter_model.safetensors \
  --local-dir /path/to/MiniMax-H3/FastH3-LoRA
```

Serve the dense artifact with the same eight-GPU pure-Ulysses topology used by the
comparison in vLLM-Omni #6909:

```bash
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \
VLLM_WORKER_MULTIPROC_METHOD=spawn \
VLLM_OMNI_VIDEO_SYNC_TIMEOUT=1800 \
vllm serve /path/to/MiniMax-H3 \
  --omni \
  --host 127.0.0.1 \
  --port 8095 \
  --trust-remote-code \
  --task-type fl2va \
  --served-model-name MiniMaxAI/MiniMax-H3 \
  --num-gpus 8 \
  --usp 8 \
  --ring 1 \
  --vae-patch-parallel-size 8 \
  --vae-parallel-mode tile \
  --vae-use-tiling \
  --diffusion-attention-backend TRTLLM_ATTN \
  --lora-path /path/to/MiniMax-H3/FastH3-LoRA/dense-datafree/adapter_model.safetensors
```

FastH3 VSA is CUDA-only and requires FastVideo's external kernel package. Install the
tested version without dependencies so it cannot replace vLLM's PyTorch/CUDA stack:

```bash
uv pip install --no-deps fastvideo-kernel==0.3.5
```

Then switch the adapter and attention backend. B300 is SM103, so the validated route
explicitly selects the Triton fallback; `FASTVIDEO_VSA_SM100A=1` requires a compatible
source-built wheel containing FastVideo's native Blackwell SM100a extension.

```bash
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \
FASTVIDEO_VSA_SM100A=0 \
VLLM_WORKER_MULTIPROC_METHOD=spawn \
VLLM_OMNI_VIDEO_SYNC_TIMEOUT=1800 \
vllm serve /path/to/MiniMax-H3 \
  --omni \
  --host 127.0.0.1 \
  --port 8095 \
  --trust-remote-code \
  --task-type fl2va \
  --served-model-name MiniMaxAI/MiniMax-H3 \
  --num-gpus 8 \
  --usp 8 \
  --ring 1 \
  --vae-patch-parallel-size 8 \
  --vae-parallel-mode tile \
  --vae-use-tiling \
  --diffusion-attention-backend FASTVIDEO_VSA \
  --fastvideo-vsa-topk 64 \
  --lora-path /path/to/MiniMax-H3/FastH3-LoRA/vsa-datafree/adapter_model.safetensors
```

FastH3 VSA applies the artifact's 50 learned compression gates to H3's packed
`[text | condition | audio | video]` sequence. Prefix tiles remain segment-pure and
dense; video queries use `(4, 4, 4)` 3-D tiles (64 tokens) and select every prefix tile
plus the top 64 video tiles. The token refiner intentionally stays dense. Pure Ulysses
(`--usp 8 --ring 1`) is supported, while Ring or AllGather sequence parallelism is
rejected because a sparse-kernel rank would not own the complete H3 block map.

Send the same fixed request to either server. Keep `num_inference_steps=4`; do not pass
`flow_shift` or `audio_flow_shift`, because the adapter must use the sigma ladder and
modality shifts it was distilled on.

```bash
curl --fail-with-body -sS -X POST http://127.0.0.1:8095/v1/videos/sync \
  -F 'prompt=A cinematic starship glides through a luminous nebula while its engines hum and distant radio chatter fills the cabin.' \
  -F 'width=1344' \
  -F 'height=768' \
  -F 'aspect_ratio=16:9' \
  -F 'fps=24' \
  -F 'num_inference_steps=4' \
  -F 'seed=1101' \
  -F 'extra_params={"task":"t2va","duration":10.0}' \
  -o fasth3.mp4
```

Check the first-forward log for `route=VSA`; a `route=SDPA` or
`FASTVIDEO_VSA falling back to SDPA` message means sparse attention did not execute.
The PR's measured B300 results used 1344×768, 24 FPS, four transformer forwards,
top-k 64, one discarded warmup, and the same fixed seed:

| Request | Metric | FastH3 Dense | FastH3 VSA | Speedup |
|---|---|---:|---:|---:|
| 10 s | Denoise loop | ~4.44 s | ~3.20 s | 1.39× |
| 10 s | Diffusion engine | 7.994 s | 5.610 s | 1.42× |
| 10 s | Server E2E incl. MP4 | 9.838 s | 7.278 s | 1.35× |
| 15 s | Denoise loop | ~8.40 s | ~4.80 s | 1.75× |
| 15 s | Diffusion engine | 11.530 s | 7.826 s | 1.47× |
| 15 s | Server E2E incl. MP4 | 14.199 s | 10.800 s | 1.31× |

Sparsity pays off more as the sequence grows: the denoise loop gains 1.39× at 10 s and
1.75× at 15 s. End-to-end lands lower than either because text encoding, VAE decoding and
MP4 muxing are a fixed cost that does not shrink with sparsity — at 15 s the non-diffusion
stage went 1.457 s → 1.648 s and muxing 1.208 s → 1.320 s. Once a route is down to four
forwards, those fixed stages, not attention, set the floor.

VSA added about 3.8 GB to the reported per-process peak in this comparison. Both paths
returned H.264 video at 24 FPS with AAC stereo audio at 32 kHz. FastH3 forbids model-level,
layerwise, and distributed layerwise offload because those paths bypass load-time fusion.

### LightX2V Turbo few-step LoRA

[LightX2V Turbo](https://huggingface.co/lightx2v/Minimax-h3-Turbo) is a family of
few-step LoRA artifacts for H3. It reaches the same goal as FastH3 by the opposite
route: instead of fusing full-rank deltas at startup, Turbo stays a standard LoRA that
each request activates by name. One server can therefore hold several artifacts and let
requests choose between them, at the cost of the LoRA matmuls on every denoise step.

Eight Diffusers-layout artifacts are supported. The filename is the contract — the server
parses the task family, step count and resolution out of it, so a renamed file is rejected
rather than served on a guess. `audio_flow_shift` is `3.0` across the family.

| Artifact | Tasks | `num_inference_steps` | `flow_shift` | alpha |
|---|---|---:|---:|---:|
| `minimax_h3_fl2v_turbo_4step_v0.1.safetensors` | T2VA / FL2VA | 5 | 12 | none → 8 |
| `minimax_h3_fl2v_turbo_4step_v1.0_768p_bf16.safetensors` | T2VA / FL2VA | 5 | 6 | 128 |
| `minimax_h3_fl2v_turbo_4step_v1.1_768p_bf16.safetensors` | T2VA / FL2VA | 5 | 6 | 128 |
| `minimax_h3_fl2v_turbo_4step_v1.2_768p_bf16.safetensors` | T2VA / FL2VA | 5 | 6 | 8 |
| `minimax_h3_fl2v_turbo_8step_v1.0_bf16.safetensors` | T2VA / FL2VA | 9 | 12 | 8 |
| `minimax_h3_fl2v_turbo_8step_v1.0_768p_bf16.safetensors` | T2VA / FL2VA | 9 | 6 | 8 |
| `minimax_h3_ref2v_turbo_4step_v0.1_bf16.safetensors` | Ref2VA | 5 | 12 | 8 |
| `minimax_h3_ref2v_turbo_8step_v1.0_768p_bf16.safetensors` | Ref2VA | 9 | 6 | 8 |

Requested sigma points are always one more than the artifact's forwards, which is why a
four-step Turbo artifact takes `num_inference_steps=5` where FastH3 takes `4`.

Every artifact is rank 128 and the delta is applied at `scale * alpha / 128`, with alpha
read from the artifact's metadata — only `4step_v0.1` declares none and falls back to 8
with a warning.

Download one artifact under the H3 root so the existing bind mount includes it. Each
artifact except `4step_v0.1` also ships a `_comfyui_` export that fuses Q/K/V into a single
projection; those are **not** supported and are refused by name.

```bash
hf download lightx2v/Minimax-h3-Turbo \
  minimax_h3_fl2v_turbo_4step_v1.2_768p_bf16.safetensors \
  --local-dir /path/to/MiniMax-H3/Turbo-LoRA
```

Serve it on the same eight-GPU pure-Ulysses topology. `--lora-path` preloads the adapter;
it accepts one artifact, or a directory holding exactly one — a directory with several
recognized artifacts is rejected as ambiguous.

```bash
CUDA_VISIBLE_DEVICES=0,1,2,3,4,5,6,7 \
VLLM_WORKER_MULTIPROC_METHOD=spawn \
VLLM_OMNI_VIDEO_SYNC_TIMEOUT=1800 \
vllm serve /path/to/MiniMax-H3 \
  --omni \
  --host 127.0.0.1 \
  --port 8095 \
  --trust-remote-code \
  --task-type fl2va \
  --served-model-name MiniMaxAI/MiniMax-H3 \
  --num-gpus 8 \
  --usp 8 \
  --ring 1 \
  --vae-patch-parallel-size 8 \
  --vae-parallel-mode tile \
  --vae-use-tiling \
  --diffusion-attention-backend TRTLLM_ATTN \
  --lora-backend peft \
  --lora-path /path/to/MiniMax-H3/Turbo-LoRA/minimax_h3_fl2v_turbo_4step_v1.2_768p_bf16.safetensors
```

Unlike FastH3, the request must activate the adapter and carry that row's sampling
contract. A request whose steps or shift do not match the loaded artifact is rejected, so
a mismatch cannot silently sample the distilled model at noise levels it never saw.

```bash
curl --fail-with-body -sS -X POST http://127.0.0.1:8095/v1/videos/sync \
  -F 'prompt=A cinematic starship glides through a luminous nebula while its engines hum and distant radio chatter fills the cabin.' \
  -F 'width=1344' \
  -F 'height=768' \
  -F 'aspect_ratio=16:9' \
  -F 'fps=24' \
  -F 'num_inference_steps=5' \
  -F 'flow_shift=6' \
  -F 'seed=1101' \
  -F 'extra_params={"task":"t2va","duration":10.0,"audio_flow_shift":3.0}' \
  -F 'lora={"name":"h3-turbo-v1.2","path":"/path/to/MiniMax-H3/Turbo-LoRA/minimax_h3_fl2v_turbo_4step_v1.2_768p_bf16.safetensors","scale":1.0}' \
  -o turbo.mp4
```

FL2VA artifacts serve `t2va` and `fl2va` on an FL2VA or combined server. The two `ref2v`
rows need `--task-type ref2va`: a combined server answers `ref2va` from a second DiT the
adapter cannot bind to, so loading one there is refused rather than running an undistilled
model on a few-step ladder. The integration is dynamic-only — no prefusion, no LoRA
composition. DLO works by keeping the request-switchable LoRA A/B buffers resident on the
accelerator while DLO streams the base blocks; model-level and standard layerwise offload
remain unsupported.

Measured on 8×B300 with the command above, at 1344×768, 24 FPS, a 15 s FL2VA request with
first and last keyframes (362 frames), fixed seed, one discarded warmup and three recorded
runs. The baseline is the same server without an adapter at `num_inference_steps=50` and
`flow_shift=12`:

| Metric | Base H3, 49 forwards | Turbo v1.2, 4 forwards | Speedup |
|---|---:|---:|---:|
| Denoise loop | ~105 s | ~9 s | 11.7× |
| Per denoise step | 2.14–2.16 s/it | 2.20–2.42 s/it | — |
| Diffusion engine | 111.1–111.3 s | 15.0–15.1 s | 7.4× |
| Server E2E incl. MP4 | 112.5–112.9 s | ~16.5 s | 6.8× |

The ~10% higher per-step cost is the LoRA matmul the fused FastH3 route does not pay —
irrelevant against a 12× cut in forwards, but the reason a single-artifact deployment can
still prefer a fused student.

These figures are for a server serving Turbo requests back to back. Alternating between
Turbo and adapter-free requests on one server additionally pays a LoRA rebind on each
switch — measured at about 24 s per switch in the same run (39.2 s diffusion engine against
15.1 s steady state), because every one of H3's 208 LoRA layers is torn down and re-uploaded.
Give Turbo its own process if the workload mixes, or track vLLM-Omni #7195, which removes
that rebind.

### RTX PRO 5000 Blackwell — four PCIe GPUs

Select the `RTX PRO 5000 Blackwell 4x` hardware profile for the validated BF16,
no-offload topology: TP2 × Ulysses2, Ring1, text-encoder TP4, VAE patch parallel 4
in tile mode, and `CUDNN_ATTN`. On the measured dual-socket host,
`CUDA_VISIBLE_DEVICES=0,2,1,3` kept the TP pairs on local PXB paths; check
`nvidia-smi topo -m` and adapt the order on other systems.

This 72 GiB/GPU profile serves the root path but selects one DiT with `--task-type`.
The task picker adds `fl2va` for T2VA/FL2VA and `ref2va` for Ref2VA; loading both DiTs
together is not qualified on this memory-constrained profile.

A five-step topology screen after warmup produced decodable H.264 video with stereo AAC:

| Workload | Client E2E | Encode | Denoise | VAE decode | Peak/GPU |
|---|---:|---:|---:|---:|---:|
| T2VA, 124 frames, 1344×768 | 32.993 s | 0.034 s | 27.915 s | 4.307 s | 67.6 GiB |
| FL2VA first frame, same output shape | 30.130 s | 0.276 s | 24.396 s | 4.339 s | 67.6 GiB |

These are topology-screening results, not 50-step throughput claims. Nsight Systems
aggregation for the same run was:

| Workload | AllGather | Send/Recv | NCCL other | NCCL total | Dense FMHA | max-min/mean |
|---|---:|---:|---:|---:|---:|---:|
| T2VA | 0.93% | 8.53% | 14.63% | 24.09% | 31.15% | 1.11% |
| FL2VA | 0.89% | 7.26% | 15.76% | 23.91% | 32.53% | 0.13% |

`NCCL other` covers NCCL kernels outside recognized AllGather and point-to-point
Send/Recv, including AllReduce, ReduceScatter, or Broadcast when present.

### AMD ROCm — four MI300X GPUs

Verified on 4× AMD Instinct MI300X (gfx942) with the ROCm H3 image, AITER packed
variable-length attention, Ulysses 4, text-encoder TP4, and native tiled VAE patch
parallelism 4:

```bash
docker pull vllm/vllm-omni-rocm:minimax-h3

HIP_VISIBLE_DEVICES=0,1,2,3 \
VLLM_ROCM_USE_AITER=1 \
VLLM_WORKER_MULTIPROC_METHOD=spawn \
VLLM_OMNI_VIDEO_SYNC_TIMEOUT=1800 \
vllm serve /path/to/MiniMax-H3 \
  --omni \
  --host 0.0.0.0 \
  --port 8000 \
  --trust-remote-code \
  --num-gpus 4 \
  --usp 4 \
  --ring 1 \
  --text-encoder-tp-size 4 \
  --vae-patch-parallel-size 4 \
  --vae-parallel-mode tile \
  --vae-use-tiling \
  --diffusion-attention-backend FLASH_ATTN
```

On ROCm, `FLASH_ATTN` resolves to AITER on supported Instinct architectures. Do not
install the CUDA-only `[fa4]` extra or set `FLASHINFER_DISABLE_VERSION_CHECK`.

The refreshed `vllm/vllm-omni-rocm:minimax-h3` tag includes the Ref2VA soundfile
fallback, TorchCodec, and FFmpeg, so T2VA, FL2VA, and the complete Ref2VA path use the
same official image.

### AMD ROCm — four MI355X GPUs

Verified on 4× AMD Instinct MI355X. The official ROCm image includes the
MiniMax H3 Ref2VA fixes, TorchCodec, and FFmpeg. We tested both Ulysses 4 with Ring 1
and Ulysses 1 with Ring 4. The command below shows the Ulysses 4 / Ring 1 configuration
with tiled VAE patch parallelism degree 4, AITER, and CPU offload:
```bash
docker pull vllm/vllm-omni-rocm:minimax-h3

HIP_VISIBLE_DEVICES=0,1,2,3 \
VLLM_ROCM_USE_AITER=1 \
VLLM_WORKER_MULTIPROC_METHOD=spawn \
VLLM_OMNI_VIDEO_SYNC_TIMEOUT=1800 \
vllm serve /path/to/MiniMax-H3 \
  --omni \
  --host 0.0.0.0 \
  --port 8000 \
  --trust-remote-code \
  --num-gpus 4 \
  --usp 4 \
  --ring 1 \
  --vae-patch-parallel-size 4 \
  --vae-parallel-mode tile \
  --vae-use-tiling \
  --enable-cpu-offload \
  --text-encoder-tp-size 4
```

This root-model command accepts T2VA, FL2VA, and Ref2VA requests without restarting.

Three constraints on this configuration:

- **Do not add `--enforce-eager`.** Regional compile is the default
  (`--diffusion-compile-granularity regional`) and is worth ~9.5%. The first request
  includes compilation — warm the server once before measuring steady state.
- **`--cfg-parallel-size` must stay 1.** H3 is CFG-distilled and has no negative branch;
  anything greater is rejected with an explicit error rather than silently duplicating a
  branch that does not exist.
- **The VAE supports its native `tile` mode only** — not `spatial_shard_width` or
  `spatial_shard_height` (those belong to the distributed Wan autoencoder). Patch
  parallel size must be 1 or the full DiT group size.

## Launch — single GPU (accuracy and memory first)

Model-level CPU offload keeps the Qwen3-VL encoder and the DiT from being resident
simultaneously. This is the accuracy-qualified reference path; it trades PCIe/NVLink
transfer latency for a much smaller GPU footprint, and needs enough system RAM for the
offloaded components.

```bash
CUDA_VISIBLE_DEVICES=0 \
VLLM_WORKER_MULTIPROC_METHOD=spawn \
VLLM_OMNI_VIDEO_SYNC_TIMEOUT=1800 \
vllm serve /path/to/MiniMax-H3 \
  --omni \
  --task-type fl2va \
  --host 0.0.0.0 \
  --port 8000 \
  --trust-remote-code \
  --num-gpus 1 \
  --enable-cpu-offload \
  --diffusion-attention-backend FLASH_ATTN
```

## Launch — DGX Spark (GB10, 128 GB unified memory)

GB10 is a **unified-memory** platform: the 128 GB LPDDR5X (≈121 GiB usable) is shared by
CPU and GPU, so both offload strategies used elsewhere in this recipe stop being capacity
levers. `--enable-cpu-offload` and distributed layerwise offload move weights from "VRAM"
to "host RAM", which is the same physical pool here — nothing is freed. DLO is actively
harmful: staging rank-local shards into pinned host memory creates a second copy
alongside the resident one, and the process is killed by the OOM killer (`Exit code: -9`)
right after `Enabling offloader backend`.

One partition on disk is 135 GiB in BF16:

| Component | Size | Quantized by `--quantization fp8` |
|---|---:|---|
| `text_encoder` (Qwen3-VL) | 63 GiB | no |
| `transformer` (DiT) | 62 GiB | yes |
| `video_vae` | 9.8 GiB | no |
| `audio_vae` | 0.6 GiB | no |

135 GiB does not fit in 121 GiB, so **FP8 is mandatory** and only one DiT can be
resident — always pass `--task-type fl2va` or `--task-type ref2va`. Online FP8 touches
the DiT only, bringing the partition to roughly 104 GiB before activations.

Weight quantization runs at load time and is slow on this platform; `--init-timeout 3600`
keeps startup from being cut short.

```bash
CUDA_VISIBLE_DEVICES=0 \
FLASHINFER_DISABLE_VERSION_CHECK=1 \
VLLM_WORKER_MULTIPROC_METHOD=spawn \
VLLM_OMNI_VIDEO_SYNC_TIMEOUT=7200 \
vllm serve /path/to/MiniMax-H3 \
  --omni \
  --task-type fl2va \
  --host 0.0.0.0 \
  --port 8000 \
  --trust-remote-code \
  --init-timeout 3600 \
  --num-gpus 1 \
  --usp 1 \
  --ring 1 \
  --quantization fp8 \
  --vae-patch-parallel-size 1 \
  --vae-parallel-mode tile \
  --vae-use-tiling \
  --enforce-eager \
  --diffusion-attention-backend CUDNN_ATTN
```

Restart with `--task-type ref2va` to serve the Ref2VA family. Keep requests at a 576 px
short edge (1024×576) and 5 s duration as the starting shape; GB10 has one GPU and no
parallelism headroom, so wall-clock per request is far higher than the multi-GPU profiles
above.

## Launch — single Ascend 950PR NPU (INT8)

vLLM-Omni serves H3 on Ascend through the `vllm-ascend` platform plugin, on a
CPU-only aarch64 PyTorch build with `torch_npu` — no CUDA runtime is present.
One 950PR holds a single DiT partition, so this profile serves the `FL2VA`
directory and uses online INT8 in place of the sharding levers the multi-GPU
profiles rely on.

Three Ascend-specific constraints come before the command:

- **Attention backend.** `CUDNN_ATTN`, `FLASHINFER_ATTN`, `TRTLLM_ATTN`, and the
SageAttention backends are CUDA-only; selecting one on NPU fails with a
`NotImplementedError` out of `forward_npu`. The NPU platform default resolves
to `FLASH_ATTN` when MindIE-SD is installed and `TORCH_SDPA` otherwise, and
`FLASH_ATTN` is where H3's NPU memory work lives: a mask-free packed varlen
path that never materializes the quadratic padding mask, plus K/V prefix
slicing. Install MindIE-SD *before* passing
`--diffusion-attention-backend FLASH_ATTN` — the backend imports `mindiesd`
lazily and otherwise raises `ImportError` at the first denoise step. Once
installed the platform default picks it up on its own.
- **Shared memory.** Decoded video crosses the diffusion-worker process boundary
through POSIX shared memory — roughly 0.9 GiB for a 5 s 1024×576 clip. The
64 MiB Docker default is large enough for the control-plane queues, so the
server starts and the denoise loop reaches 100% before the write into
`/dev/shm` raises SIGBUS and kills the worker with `Bus error (core dumped)`.
Check `df -h /dev/shm` and start the container with `--shm-size=8g` or
`--ipc=host`. It cannot be changed on a running container, and an in-container
`mount -o remount` needs `CAP_SYS_ADMIN`.
- **No `--enforce-eager`.** It disables regional compilation and only slows the
steady state; the first request is a warmup either way. `--cfg-parallel-size`
must stay 1, as everywhere else in this recipe.

```bash
ASCEND_RT_VISIBLE_DEVICES=0 \
VLLM_WORKER_MULTIPROC_METHOD=spawn \
VLLM_OMNI_VIDEO_SYNC_TIMEOUT=14400 \
vllm serve /path/to/MiniMax-H3/FL2VA \
  --omni \
  --host 0.0.0.0 \
  --port 8000 \
  --trust-remote-code \
  --num-gpus 1 \
  --tensor-parallel-size 1 \
  --usp 1 \
  --ring 1 \
  --text-encoder-tp-size 1 \
  --vae-patch-parallel-size 1 \
  --vae-parallel-mode tile \
  --vae-use-tiling \
  --init-timeout 14400 \
  --stage-init-timeout 14400 \
  --quantization int8
```

## Text-encoder tensor parallelism

By default the whole Qwen3-VL encoder is resident on the DiT main rank, making it the
peak-memory hotspot on multi-GPU no-offload runs. `--text-encoder-tp-size N` shards it
across the first `N` DiT ranks using vLLM-style tensor-parallel layers on a dedicated
encoder process group:

```bash
vllm serve /path/to/MiniMax-H3 \
  --omni --trust-remote-code \
  --task-type fl2va \
  --num-gpus 4 --usp 4 --ring 1 \
  --text-encoder-tp-size 4 \
  --vae-patch-parallel-size 4 --vae-parallel-mode tile --vae-use-tiling
```

- `N` must divide the Qwen3-VL head counts (64 attention heads / 8 KV heads) → 1, 2, 4 on
  a 4-GPU server; 1, 2, 4, 8 on 8 GPUs.
- On 4 GPUs, `N=4` drops the per-rank engine peak from **133 GB to 103 GB** (~35.7 GB) at
  no measurable throughput cost, while each non-main rank gains ~51.5/N GB.
- Numerically: `N=1` reproduces the HF reference path bit-exactly (`max_abs = 0`); `N=4`
  introduces bounded BF16 rounding only (row-parallel all-reduce in FP32), giving
  31.11 dB PSNR / 0.9566 SSIM end-to-end against `N=1` with identical structure.
- Combining `N>1` with regional compile used to produce a `ValueError: v must be finite`
  on the *second* request. That is fixed — the token refiner's attention runs on
  replicated rows before `sp_prepare` and was wrongly getting a Ulysses SP all-to-all.

## Key request parameters

| Parameter | Recommended | Notes |
|---|---|---|
| `task` | `t2va`, `fl2va`, `ref2va` | Passed inside `extra_params`; must be compatible with `--task-type` when one is set |
| `duration` | 4–15 s | Decimal seconds in `extra_params`; snapped to H3's legal `17n+5` frame count (15 s → 362 frames / 15.083 s) |
| `fps` | `24` | Output FPS is fixed |
| `num_inference_steps` | `50` | Matches the reference accuracy workloads |
| `flow_shift` | `12` | Video sigma shift |
| `audio_flow_shift` | `3` | Audio sigma shift, in `extra_params` |
| `width`, `height` | see below | Server-side validation: multiples of 32, aspect ratio 1:4 to 4:1 |
| `seed` | fixed value | Output is deterministic per request at a fixed seed |

On `width`/`height`, the serving-side check is broader than the documented product modes —
pick a **short edge of 768 or 1440** in one of the supported ratios (21:9, 16:9, 4:3, 1:1,
3:4, 9:16) to stay on shapes the model was trained and validated for. FL2VA can omit both
and inherit the first frame's aspect ratio at a 768 px short edge. Prompts cap at 7,000
characters.

Use `POST /v1/videos/sync` when you want the MP4 in the response body; `POST /v1/videos`
is the async job-polling variant.

## Validated performance (4×B300)

These measurements used task-specific single-DiT processes before the modular
combined-service merge; they remain the validated per-task operating points.

| Workload | Config | Result |
|---|---|---|
| FL2VA, 209 frames, 1248×768 (8.7 s) | no offload, U4, VPP4 tile, regional compile | **86.96 s** client E2E (±0.23 s) |
| Two-video Ref2VA, 362 frames, 1344×768 (15 s) | same | **784.4 s** accounted model-stage mean |

Stage split for the FL2VA case: text encoder 0.21 s, visual encoder 0.20 s, DiT 79.1 s
(**88%** of the request), video+audio VAE decode 2.40 s. VAE patch parallelism is the
cheapest win available — it cuts decode 3.4–3.5× (8.24 s → 2.40 s).

### Validated performance (4×MI300X)

Measured with `vllm/vllm-omni-rocm:minimax-h3`
(`sha256:29d1946af9c69e3e0a7128c247bdc8c82437ead43fbc366fed443533cf6ce9e8`),
BF16, AITER, U4, text-encoder TP4, VPP4 tile, regional compile, one warmup, and
the synchronous Video API:

| Workload | Model stages | Client E2E |
|---|---:|---:|
| T2VA, 209 frames, 1344×768 (8.7 s) | encode 0.09 s, denoise 244.04 s, decode 4.15 s | **267.42 s** |
| FL2VA, 209 frames, 1344×768 (8.7 s) | encode 13.98 s, denoise 257.58 s, decode 4.11 s | **287.07 s** |

Both outputs were validated as 209-frame H.264 at 24 FPS with 32 kHz stereo AAC.
A v0.26.0 source build additionally completed image+audio and video-reference Ref2VA,
plus a 2048×1088 T2VA run. These measurements describe tested shapes, not a general
latency guarantee.

A `with_stack` profile of the two-video Ref2VA case shows the pipeline is
**GPU-attention-bound, not host-starved**: FlashAttention-4 is ~76% of `diffuse` device
time, Ulysses send/recv is secondary, GPU utilization is 92.8–94.3% and the CPU-idle
union inside the transformer forwards is 3.15%. Reducing DiT time further needs a change
in attention cost, which is not lossless;
[SAGE + Skip-Softmax](#optional-lossy-attention-sage--skip-softmax) above is the supported
way to trade attention precision and sparsity for speed on datacenter Blackwell.

Against the checkpoint's own reference implementation, the accuracy-qualified path scores
SSIM 0.9873–0.9896, PSNR 39–42 dB, pixel cosine 0.9996+, and audio log-mel cosine
0.977–0.996 on T2VA/FL2VA. The two-video Ref2VA composite scores lower on raw pixels
(SSIM 0.628) largely because of H.264 bitrate differences, while CLIP cosine is 0.9816 and
audio log-mel cosine 0.9869 — content and motion stay aligned.

### Cache-DiT

Opt-in only. A 50-step T2VA probe cut denoise time **30.2%** (121.01 s → 84.47 s) but
dropped SSIM to **0.831** against the uncached output. H3's video trajectory is sensitive
to cached denoise steps — choose the cache budget against your own quality target.

### Offload and accuracy

On a **single GPU**, model-level CPU offload is the accuracy-qualified path. On four GPUs,
no-offload is both faster and accuracy-clean once the encoder stays resident: an earlier
no-offload build performed a ~63 GiB DiT GPU→CPU→GPU round trip inside `encode_prompt`,
which was pure overhead (encoder 30.38 s → 0.207 s once removed, ~146×).

## Known limitations

- The modular root service loads both DiTs by default. Capacity profiles use
  `--task-type fl2va` or `--task-type ref2va` and expose only that task family.
- H3 executes **one generation request per diffusion batch** today.
- The first request is a compile warmup only on the resident regional-compile profiles.
  The two-GPU DLO recipes use `--enforce-eager` and do not compile the DiT.
- **The serving path accepts fewer references than the model supports.** H3 documents up
  to 9 images, 3 video clips, and 3 audio clips (12 files) per Omni Reference request;
  the current vLLM-Omni path takes exactly one image plus one audio reference, **or** one
  or more videos with **no** separate `audio_reference` (it uses the source soundtracks).
- The 768 px short-edge mode is available for T2VA and FL2VA; 1344×768 is the documented
  16:9 request shape. The 24 GB DLO recipe uses 1024×576 as its lower-memory starting
  point.
- `--cfg-parallel-size > 1` is rejected by design (CFG-distilled, no negative branch).
- The VAE supports the native `tile` parallel mode only.
- A `U2 × Ring2` hybrid currently fails with an attention-mask length mismatch; use pure
  Ulysses.
- FastH3 preview v1 serves T2VA only, is fused at startup, and cannot use model-level,
  layerwise, or distributed layerwise offload. Its VSA variants additionally require CUDA,
  `fastvideo-kernel`, `FASTVIDEO_VSA`, and pure local/Ulysses attention; Ring and AllGather
  sequence parallelism are rejected.
- Online FP8 quantizes the DiT only — the Qwen3-VL text encoder and both VAEs stay
  BF16. It is incompatible with layerwise offload, whose weight stride is rejected by
  the Cutlass FP8 kernel.
- On unified-memory platforms (DGX Spark / GB10) CPU offload and DLO give no capacity
  benefit, because host RAM and VRAM are one pool; DLO additionally doubles peak usage
  while staging. Use resident FP8 with a single `--task-type` partition instead.
- Pure Ulysses still replicates the 66.3 GB DiT on every rank, so 64 GB GPUs cannot use
  `--usp N --tp 1` as a resident capacity path. Use DiT TP plus DLO (the two-GPU recipes
  above), or model-level CPU offload; text-encoder TP alone is not sufficient.
- DLO is a capacity path, not a free speedup: it needs substantial host RAM and streams
  weights over PCIe on every denoise step. Increase `--dlo-resident-layers` only after
  checking peak HBM on the exact request shape.

## References

- [Model card](https://huggingface.co/MiniMaxAI/MiniMax-H3)
- [Modular FL2VA/Ref2VA pipeline (vLLM-Omni #5720)](https://github.com/vllm-project/vllm-omni/pull/5720)
- [vLLM-Omni Video API](https://docs.vllm.ai/projects/vllm-omni/en/latest/serving/videos_api/)
- [vLLM-Omni diffusion parallelism](https://docs.vllm.ai/projects/vllm-omni/en/latest/user_guide/diffusion/parallelism/overview/)
- [vLLM-Omni diffusion attention backends](https://docs.vllm.ai/projects/vllm-omni/en/latest/user_guide/diffusion/attention_backends/)
- [FastVideo VSA attention guide](https://docs.vllm.ai/projects/vllm-omni/en/latest/user_guide/diffusion/attention_backends/fastvideo_vsa/)
- [FastH3 preview adapters](https://huggingface.co/FastVideo/FastVideo-FastH3-4-step-Preview-v1-LoRA)
- [MiniMax-H3 VSA and Ulysses support (vLLM-Omni #6909)](https://github.com/vllm-project/vllm-omni/pull/6909)
- [vLLM-Omni TRTLLM attention guide (SAGE, Skip-Softmax)](https://docs.vllm.ai/projects/vllm-omni/en/latest/user_guide/diffusion/attention_backends/trtllm/)
- [MiniMax-H3 production serving on vLLM-Omni (blog)](https://vllm.ai/blog/2026-09-01-minimax-h3-production-serving)
- [vLLM-Omni supported models](https://docs.vllm.ai/projects/vllm-omni/en/latest/models/supported_models/)
- [vLLM-Omni GPU installation](https://docs.vllm.ai/projects/vllm-omni/en/latest/getting_started/installation/gpu/)
- [ROCm H3 tracking issue](https://github.com/vllm-project/vllm-omni/issues/5697)
