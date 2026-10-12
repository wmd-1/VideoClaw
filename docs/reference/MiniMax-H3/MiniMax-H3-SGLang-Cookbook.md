# MiniMax-H3

> Run native MiniMax-H3 video-and-audio generation with SGLang Diffusion.

> Saved locally from https://docs.sglang.io/cookbook/diffusion/MiniMax/MiniMax-H3 on 2026-09-29 (raw MDX source).
> This page is interactive: the `<Tabs>` panels, the `<Deployment>` recipe builder and the
> benchmark tables below are all in this file. The selectable options and the data behind
> those tables are not written as prose anywhere else, so they are kept verbatim in
> [Appendix A](#appendix-a-mdx-components-and-recipe-data) at the end.

<DiffusionModelTags tags={["video + audio", "T2VA / FL2VA / Ref2VA", "multimodal references", "4–15 seconds", "768p"]} />

## 1. Quick start

Install with `uv pip install "sglang[diffusion]" --prerelease=allow`, then choose
a verified recipe below. Setup changes the deployment; Server and Request expose
orthogonal startup and sampling choices.

<Deployment config={config} />

<Note>
  The generated Server command already includes the recommended encoder policy.
  Change a Server option only for a deliberate trade-off; Request options do not
  reload the model.
</Note>

The Docker form installs the platform-specific diffusion extra from the source
bundled in the image. For conditioned requests, set **Host media directory**
under **Variables**; the builder mounts it read-only at `/data/minimax-h3`.
AMD and Grace Blackwell currently offer the Python form; the other NVIDIA
recipes also offer Docker.

To use ModelScope through the same normal `sglang serve` path, prefix the copied
command with `SGLANG_USE_MODELSCOPE=true` and replace the model path with
`MiniMax/MiniMax-H3`. Keep the selected variant and topology flags unchanged.

For platform-specific installation details, see the
[SGLang Diffusion installation guide](/docs/sglang-diffusion/installation).

## 2. Model capabilities

[MiniMax-H3](https://huggingface.co/MiniMaxAI/MiniMax-H3) is a native joint video-and-audio model for text-to-video-and-audio, first/last-frame control, and multimodal reference conditioning. Its main strength is producing the picture and stereo soundtrack together, so speech, music, ambient sound, and visible events can stay aligned without a separate audio-generation pass.

Choose H3 when synchronized audiovisual output or reference-driven generation matters more than a lightweight deployment. The released recipe targets a 768-pixel short edge at 24 fps for 4–15 seconds, and its capabilities are split across two checkpoint partitions; serving every mode therefore requires separate FL2VA and Ref2VA deployments.

| Task | `task` value | Conditioning |
| - | - | - |
| Text to video and audio | `t2va` | Text prompt only |
| First/last frame to video and audio | `fl2va` | First frame, last frame, or both |
| Reference to video and audio | `ref2va` | Image, video, and audio references, optionally combined with first/last keyframes |

Video-to-video (V2V) is a supported `ref2va` use case, not a fourth task
value. Run the `Ref2VA` partition and provide a video reference in
`conditions`. A hybrid `ref2va` request may also include the same ordered
first/last keyframes accepted by `fl2va`, but it must still contain at least one
reference condition.

Use the selected Hub's root model ID: `MiniMaxAI/MiniMax-H3` on Hugging Face
or `MiniMax/MiniMax-H3` on ModelScope. Select the checkpoint variant with
`--model-variant`: `fl2va` serves both `t2va` and `fl2va`, while `ref2va`
serves reference-conditioned requests. SGLang owns the checkpoint-directory
mapping; do not point `--model-path` at a manually downloaded subdirectory.
The same FL2VA / Ref2VA split applies in
[ComfyUI integrated mode](#comfyui-integrated-mode).

<Warning>
  Review the license and usage terms in the MiniMax-H3 model card before production or commercial use. SGLang support does not grant additional model usage rights.
</Warning>

## 3. Deployment details

The builder accepts legal custom GPU counts and topologies, marking them
**Unverified** until the exact recipe has completed end-to-end validation.
Static H3 head or partition violations disable Copy before they reach
`sglang serve`.

### Grace Blackwell

Select **GB300** or **GB200** in the builder. The default is one Linux ARM64
host with four GPUs, Ulysses4, and resident weights. This follows the
[four-GPU compute-tray layout](https://docs.nvidia.com/dgx/dgxgb200-user-guide/hardware.html);
an NVL72 rack is not a single 72-GPU process host.

For two four-GPU hosts, set **Nodes = 2** and **GPUs / node = 4**. The builder
emits Ulysses4 inside each host, Ring2 across hosts, and replicated encoders.
Set the same reachable **Head address** on both hosts and use node ranks 0 and

1. Run the generated command once per host, with the same SGLang revision and
   dependencies. Let `sglang serve` resolve and download the selected weights.

GB200 is a **derived, unverified recipe**, not a measured GB300-equivalent
performance claim. Keep the native precision and platform-default attention
for the baseline; the B200/B300 quantization measurements and H200 Cache-DiT
quality audit do not establish coverage on Grace Blackwell.

GB300 end-to-end coverage currently includes single-host FL2VA text-to-video
with audio, native precision, eager execution, and `quality="lossless"`.
Ref2VA and multi-host commands remain **Unverified**. See the
[GB300 measurements](#gb300-single-host) for the tested workload.

### Checkpoint and adapter formats

Start with the command emitted by the builder. Every row below is an overlay on
the same native SGLang pipeline; component repositories contribute their own
config and weights, while weight files retain the base component config. Storage
layout and inference behavior are separate contracts: for example, a PEFT file
may be either a normal style adapter or a timestep-distilled Turbo adapter.

| Scope | Format or variant | Add to the base command | Contract |
| - | - | - | - |
| Full model | Official mixed BF16/FP32, CFG-distilled | `--model-variant fl2va` or `--model-variant ref2va` | Lossless reference and consistency GT path. CFG distillation removes the negative branch; it is not the few-step timestep distillation used by Turbo releases. |
| DiT | Official Diffusers component layout | `--component-paths.transformer MiniMaxAI/MiniMax-H3/transformer` (`fl2va`) or `.../transformer_ref` (`ref2va`) | Loads the official component through the native SGLang graph; no Diffusers runtime fallback. |
| DiT | [AdaLN-pruned Diffusers component](https://huggingface.co/multimodalart/MiniMax-H3-Pruned) | `--component-paths.transformer multimodalart/MiniMax-H3-Pruned/transformer` or `.../transformer_ref` | Approximate curve-AdaLN architecture; its config and basis metadata are loaded natively. |
| DiT | [Full or AdaLN-pruned](https://huggingface.co/Comfy-Org/MiniMax-H3/tree/main/diffusion_models), or [LoRA-merged/remixed](https://huggingface.co/FX-FeiHou/MiniMax-H3-Remix) BF16 safetensors | `--component-weights-paths.transformer OWNER/REPO/path/FILE.safetensors` | Weight-only override for a native full/pruned H3 layout. Match the FL2VA/Ref2VA partition; pruned, merged, or dtype-converted exports are approximate, and any author-specific sampler remains a separate requirement. |
| DiT | FL/Ref hybrid fine-tunes, including [Singularity v1.3](https://huggingface.co/WarmBloodAban/Minimax-h3_Singularity) (full or pruned INT8) | Replace `--model-variant` with `hybrid`; add `--component-weights-paths.transformer OWNER/REPO/FILE.safetensors` | Explicit merged-weight deployment serving `t2va`, `fl2va`, and `ref2va` on one native pipeline. Quantization and pruning are detected from the file; INT8 requires `comfy-kitchen`. See [Singularity](#singularity-hybrid-weights). |
| DiT | Comfy FP8 or self-describing MXFP8 safetensors | `--component-weights-paths.transformer OWNER/REPO/path/FILE.safetensors` | Per-layer metadata selects static/dynamic FP8 or MXFP8 automatically. |
| DiT | ConvRot INT8, W4A8, W4A4, or mixed W4A4+INT8 safetensors ([INT8](https://huggingface.co/Comfy-Org/MiniMax-H3/tree/main/diffusion_models), [W4A8](https://huggingface.co/Winnougan/MiniMax-H3-INT4_Convrot_ComfyUI), [W4A4](https://huggingface.co/Merserk/MiniMax-H3-INT4-ConvRot)) | `--component-weights-paths.transformer OWNER/REPO/path/FILE.safetensors` | Auto-detected; requires `comfy-kitchen`. TP must preserve each file's ConvRot group boundaries. |
| DiT | [NVFP4, optionally mixed with INT8 or FP8](https://huggingface.co/Abiray/Minimax-H3-nvfp4-INT4-INT8-Convrot) | `--component-weights-paths.transformer OWNER/REPO/path/FILE.safetensors` | Auto-detected; NVFP4 execution requires NVIDIA compute capability 10.0+. |
| DiT | [AutoRound W4A16 component](https://huggingface.co/Ar4ikov/MiniMax-H3-transformer-W4A16-RTN) | `--component-paths.transformer Ar4ikov/MiniMax-H3-transformer-W4A16-RTN` | Self-describing Diffusers component; SGLang reuses the SRT GPTQ/Marlin backend. The linked export is FL2VA. |
| DiT | GGUF, full or AdaLN-pruned ([full](https://huggingface.co/leejet/MiniMax-H3-GGUF), [pruned](https://huggingface.co/unsloth/MiniMax-H3-GGUF)) | `--component-weights-paths.transformer OWNER/REPO/FILE.gguf` | CUDA capacity path; aligned TP and layerwise offload are supported, FSDP and LoRA are not. |
| Text encoder | Architecture-compatible Qwen3-VL BF16 finetune ([Heretic example](https://huggingface.co/llmfan46/Qwen3-VL-32B-Instruct-ultra-uncensored-heretic)) | `--component-paths.text_encoder OWNER/REPO` | Reuses the native H3 extractor, including its vision tower and layer-50 hidden-state selection. Finetuning changes conditioning, not the sampling schedule. |
| Text encoder | [Serialized FP8 component](https://huggingface.co/Qwen/Qwen3-VL-32B-Instruct-FP8) | `--component-paths.text_encoder Qwen/Qwen3-VL-32B-Instruct-FP8` | Only eligible language-model linears use FP8; embeddings, norms, and the vision tower keep their declared precision. |
| Text encoder | ConvRot INT8, W4A8, or W4A4 safetensors ([INT8](https://huggingface.co/Comfy-Org/MiniMax-H3/tree/main/text_encoders), [Heretic INT8](https://huggingface.co/ethanfel/Qwen3-VL-32B-Ultra-Heretic-H3-ComfyUI-INT8-ConvRot), [W4A8](https://huggingface.co/Winnougan/MiniMax-H3-INT4_Convrot_ComfyUI), [W4A4](https://huggingface.co/Merserk/MiniMax-H3-INT4-ConvRot)) | `--component-weights-paths.text_encoder OWNER/REPO/path/FILE.safetensors` | Auto-detected; requires `comfy-kitchen`. INT8/W4A8 files may include a scalar-scale INT8 embedding. Unmarked tensors retain their declared precision. |
| Text encoder | Comfy NVFP4 with dynamic activation quantization ([Heretic example](https://huggingface.co/sakamakismile/Qwen3-VL-32B-Heretic-MiniMax-H3-NVFP4)) | `--component-weights-paths.text_encoder OWNER/REPO/path/FILE.safetensors` | Requires `comfy-kitchen` and NVIDIA compute capability 10.0+. Per-layer metadata selects native NVFP4 matmul; scalar/row-wise INT8 embeddings remain packed. |
| Text encoder | [NVFP4-AWQ](https://huggingface.co/Comfy-Org/MiniMax-H3/tree/main/text_encoders) or [Quanto qint8](https://huggingface.co/DeepBeepMeep/MiniMax-H3/tree/main/Qwen3-VL-32B-Instruct) safetensors | `--component-weights-paths.text_encoder OWNER/REPO/path/FILE.safetensors` | Memory-oriented formats: compressed storage is restored, then each active matrix uses BF16/FP16 compute. |
| Text encoder | [GGUF Qwen3-VL](https://huggingface.co/DeepBeepMeep/MiniMax-H3/tree/main/Qwen3-VL-32B-Instruct) | `--component-weights-paths.text_encoder OWNER/REPO/FILE.gguf` | CUDA capacity path with encoder TP/layerwise support; encoder FSDP is not supported. |
| Text encoder | [Compact Qwen3-VL 4B/8B + ClipProj](https://huggingface.co/NicoLab28/ClipProj-MiniMax-H3) | `--component-paths.text_encoder ENCODER_REPO --component-paths.conditioning_projection PROJECTION.safetensors` | Approximate conditioning replacement. A separate weight-only override may quantize the selected small encoder. |
| DiT or adapter | Timestep-distilled Turbo, as merged weights or LoRA ([Larry](https://huggingface.co/larryvrh/MiniMax-H3-Turbo-Lora), [LightX2V](https://huggingface.co/lightx2v/Minimax-h3-Turbo), [merged Ref2VA INT8 example](https://huggingface.co/PulpCut/MiniMax-H3-Ref2VA-Turbo-INT8-ConvRot)) | Use `--component-weights-paths.transformer ...` for merged weights, or `--lora-path OWNER/REPO --lora-weight-name FILE --lora-merge-mode auto` for LoRA | Few-step semantic variant. Pin the exact FL2VA/Ref2VA file and its NFE/sigma schedule, scale, and alpha; storage-format detection does not infer sampling behavior. See [LoRA recipes](#5-lora-recipes). |
| Adapter | Style, subject, or behavior LoRA ([example](https://huggingface.co/fal/MiniMax-H3-Realism-People-LoRA)) | `--lora-path OWNER/REPO [--lora-weight-name FILE] --lora-merge-mode auto` | Native fused and Diffusers/PEFT layouts are normalized at load time. Keep the base schedule unless the author specifies another one, and preserve any trigger phrase, scale, and alpha metadata. |

The rows compose rather than enumerate every cross-product. A Turbo-merged INT8
ConvRot checkpoint, for example, must satisfy both the Turbo sampling contract
and the ConvRot storage/backend contract.

H3's Qwen3-VL text encoder also handles image understanding; there is no separate
`image_encoder` component. Select the conditioning checkpoint, not an optional
generation-tail file for prompt rewriting. The `uncensored` or `Heretic` label
describes a weight modification, not a separate loader or a guarantee of output quality.

For H3, the registered component names are `transformer`, `text_encoder`,
`video_vae`, and `audio_vae`. The shorter `--transformer-weights-path` and
`--text-encoder-path` aliases remain supported. `conditioning_projection` is an
H3 text-encoder sidecar key, not a standalone model component. Plain
video/audio VAE safetensors can use `--component-weights-paths.video_vae` or
`--component-weights-paths.audio_vae`, but SGLang does not currently advertise
a native quantized H3 VAE format.

Pre-quantized files are self-describing: do not combine those rows with
`--quantization` or `--component-quantizations.*`. Only byte-identical official
full weights—whether loaded from the original model, Diffusers component, or a
weight-only layout—belong to the consistency GT. Pruned, quantized,
compact-encoder, and LoRA routes are outside that baseline and the audited
`quality="high"` contract. Packed and per-layer mixed formats reject FSDP unless
their row says otherwise; see
[Quantization](/docs/sglang-diffusion/quantization) for backend-wide constraints.

LoRA tensors alone do not make an execution-coupled release portable. Sparse-
attention/SLA adapters and causal-streaming adapters such as RAVEN also require
their matching attention or streaming pipeline; they are not standard H3 LoRA
overlays in SGLang. Likewise, a remixed checkpoint that prescribes a custom
sampler is only covered when that sampler contract can be reproduced—the fact
that its safetensors layout loads is not sufficient.

For a four-card H200 host, keep the full BF16/FP32 model resident by default.
The model fits without FSDP, so this path avoids the per-block parameter
all-gathers of the memory-oriented FSDP profile:

```bash 4×H200 resident theme={null}
sglang serve \
  --model-path MiniMaxAI/MiniMax-H3 \
  --model-variant fl2va \
  --num-gpus 4 \
  --ulysses-degree 4 \
  --encoder-parallel auto \
  --performance-mode speed \
  --port 30010
```

Pure Ulysses4 is also the faster measured topology on H200, not just a
capacity default. The 4×H100 TP2 + Ulysses2 recipe below fits on 141 GB H200
cards, but it replaces the Ulysses all-to-all exchange with two per-block
tensor-parallel all-reduces and measured slower end-to-end, at about 30 GB
lower peak memory per GPU. See the **H200 topology comparison** in the
Benchmarks section for the measured numbers; treat TP2 + Ulysses2 on H200 as
a deliberate memory trade, not a latency default.

For 4×H100 80 GB, balance the large packed activation with resident weight
sharding. TP2 + Ulysses2 was the fastest measured lossless topology while the
Qwen encoder still folds across all four GPUs:

```bash 4×H100 fastest theme={null}
sglang serve \
  --model-path MiniMaxAI/MiniMax-H3 \
  --model-variant fl2va \
  --num-gpus 4 \
  --tp-size 2 \
  --ulysses-degree 2 \
  --encoder-parallel auto \
  --performance-mode speed \
  --port 30010
```

Pure Ulysses4 could not keep the full pipeline resident on 80 GB H100s. Use
`--tp-size 4 --ulysses-degree 1` when lower resident memory matters more than
the last few percent of latency. FSDP remains a verified capacity option, but
its per-block weight all-gathers do not make it the H100 speed default:

```bash 4×H100 FSDP capacity theme={null}
sglang serve \
  --model-path MiniMaxAI/MiniMax-H3 \
  --model-variant fl2va \
  --num-gpus 4 \
  --ulysses-degree 4 \
  --encoder-parallel auto \
  --performance-mode speed \
  --use-fsdp-inference true \
  --port 30010
```

For a two-card RTX 5090 host, use TP2 and keep 20 DiT blocks
resident. Layerwise placement is lossless: it changes parameter placement and
transfer scheduling, not the BF16/FP32 denoising or VAE math. This is the
fastest measured 32 GB operating point:

```bash 2×RTX 5090 fastest lossless theme={null}
sglang serve \
  --model-path MiniMaxAI/MiniMax-H3 \
  --model-variant fl2va \
  --num-gpus 2 \
  --tp-size 2 \
  --ulysses-degree 1 \
  --encoder-parallel auto \
  --performance-mode memory \
  --layerwise-offload-components dit,text_encoder,vae \
  --dit-offload-prefetch-size 1 \
  --dit-layerwise-resident-layers 20 \
  --enable-torch-compile false \
  --port 30010
```

The DiT residency and prefetch knobs apply only to the repeatedly executed DiT
blocks. The text encoder and the video VAE decoder blocks use one-layer
prefetch with zero resident layers. The video VAE encoder stays resident
because its indexed down blocks cannot host executable layerwise hooks; the
roughly 577 MiB audio VAE also stays resident because offloading it only adds
transfer overhead. This exact recipe was validated on
2× RTX 5090 (32 GB each) and a 377 GiB host; use a 384 GiB-class machine. The
latency and memory comparison is collected in the benchmark section below.

For a single 24 GB consumer card (RTX 4090), stream the DiT and text encoder
and quantize DiT linear layers online with `kitchen_int8`. Keep `vae` out of
`--layerwise-offload-components`: putting the VAE decoder in layerwise
offload re-streams about 9 GiB on each of 167 decode tiles. Default
attention stays `fa` (exact). Approximate backends are opt-in; see
[Attention Backends](/docs/sglang-diffusion/attention_backends#sage-then-sol-hybrid).
Install `comfy-kitchen` first (`pip install comfy-kitchen`).

```bash 1×RTX 4090 24GB theme={null}
sglang generate \
  --model-path MiniMaxAI/MiniMax-H3 \
  --model-variant fl2va \
  --quantization kitchen_int8 \
  --attention-backend fa \
  --performance-mode memory \
  --layerwise-offload-components dit,text_encoder \
  --dit-offload-prefetch-size 1 \
  --dit-layerwise-resident-layers 0 \
  --enable-torch-compile false \
  --prompt "A cat walking on a sunny beach, gentle waves." \
  --save-output
```

The same flags work on `sglang serve`. Drop `--quantization` for the BF16
baseline; everything else stays identical. GPU peak stays about 18 GB
either way because streaming offload is set by the offload buffers and VAE
decode, not the weight dtype.

To skip DiT steps on 4090 while keeping `fa`, add Spectrum to the generate
command above. Do not combine it with `quality="high"` or Cache-DiT.
On this card, 50-step BF16+FA (**1008.6 s**) drops to **285.3 s (3.53×)**
with exact attention, up to **219.0 s (4.61×)** with Sage→Sol hybrid +
Spectrum. See the 4090 table in Benchmarks.

```bash theme={null}
  --enable-spectrum \
  --spectrum-warmup-steps 11 \
  --spectrum-window-size 5 \
  --spectrum-flex-window 1.0
```

The first launch resolves every selected source through the normal Hub path. If
a repository requires authentication, export a Hugging Face token in the
server environment; no manual pre-download is required.

For MiniMax-H3, `--performance-mode speed` deliberately keeps the DiT eager.
The current `torch.compile` path changes the model's numerical output, so no
recommended lossless preset enables it implicitly. An explicit
`--enable-torch-compile true` remains available for controlled experiments, but
do not use it to generate consistency ground truth.

### ComfyUI integrated mode

The [ComfyUI plugin](/docs/sglang-diffusion/comfyui) can run H3 as a per-step
DiT backend. ComfyUI keeps CLIP, VAE, and the sampler loop. SGLang loads only
the transformer under `--comfyui-mode`.

Copy `python/sglang/multimodal_gen/apps/ComfyUI_SGLDiffusion` into ComfyUI
`custom_nodes/`. Load a shipped graph and set `SGLDOptions.model_type` to
`minimax_h3`:

| Graph | Task | DiT filename |
| - | - | - |
| `minimax_h3_t2v_sgld.json` | T2V / I2VA / FL2VA | `minimax_h3_fl2va_*.safetensors` |
| `minimax_h3_r2v_sgld.json` | Reference-to-video | `minimax_h3_ref2va_*.safetensors` |
| `minimax_h3_t2v_sgld_upscaler.json` | Low-res then 3D ×2 | `fl2va` DiT |

Pruned BF16 / FP8 files from [Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3/tree/main/diffusion_models)
are the usual loader inputs. GGUF goes in `SGLDOptions.transformer_weights_path`.
On a 24 GB card put the 32B CLIP on CPU and enable DiT layerwise offload; use
`kitchen_int8` or GGUF rather than the full unpruned BF16 DiT.

### Singularity hybrid weights

[Singularity](https://huggingface.co/WarmBloodAban/Minimax-h3_Singularity) is an
FL/Ref fusion fine-tune, not a complete pipeline repository. Keep the official
model ID for the encoders and VAEs, and select one exact transformer file:

```bash 2×B300 hybrid checkpoint theme={null}
pip install comfy-kitchen

WEIGHTS=WarmBloodAban/Minimax-h3_Singularity/Minimax-h3_Singularity_ref2va_Pruned_v1.3_int8.safetensors
sglang serve --model-path MiniMaxAI/MiniMax-H3 \
  --model-variant hybrid \
  --component-weights-paths.transformer "$WEIGHTS" \
  --num-gpus 2 --ulysses-degree 2 --performance-mode speed \
  --port 30000
```

For the full INT8 checkpoint, replace the filename with
`Minimax-h3_Singularity_ref2va_v1.3_int8.safetensors`. SGLang downloads the
selected file automatically. Do not add an online quantization override.
`hybrid` requires an explicit weight override; it does not convert the official
Ref2VA weights into a multimode model. Request `task` and `conditions` retain
their [usual meanings](#4-generate-video-and-audio), including `conditions: []`
for `t2va` and keyframes for `fl2va`.

Start with `quality: "lossless"` and the standard 50-point schedule. Here
`lossless` disables additional request-time approximations; it does not undo
INT8 quantization, pruning, or fine-tuning. The author's recommended Ref2V
Turbo LoRA is a separate optional adapter, not an automatically inferred
4-step schedule. Reference fidelity can differ from the official model; see
the [author's guidance](https://huggingface.co/WarmBloodAban/Minimax-h3_Singularity/discussions/1)
before applying the [LoRA sampling contract](#5-lora-recipes).

### Advanced: precomputed AdaLN cache

The [model card](https://huggingface.co/MiniMaxAI/MiniMax-H3) notes that about
13B H3 parameters are AdaLN branches whose outputs can be precomputed for
inference. The public base checkpoint contains the original branches, not a
ready-to-use cache. SGLang therefore keeps the standard path as the default.

<Warning>
  This is an experimental deployment path. It is intentionally disabled unless
  you provide an explicitly generated cache; end-to-end numerical and peak-memory
  validation remains required before using it in production.
</Warning>

When an inference-only deployment has a fixed sampling schedule, build a cache
from the already materialized transformer directory on CUDA, then pass it to
the usual `sglang serve` command. This does not alter the denoising formula:
the cache stores the BF16 outputs of the original AdaLN linears.

```bash Command theme={null}
python -m sglang.multimodal_gen.tools.build_minimax_h3_adaln_cache \
  --transformer-path "$TRANSFORMER_PATH" \
  --model-variant fl2va \
  --mode t2va \
  --num-inference-steps 50 \
  --flow-shift 12 \
  --audio-flow-shift 3 \
  --output /models/minimax-h3-fl2va-adaln-50step.safetensors

sglang serve \
  --model-path MiniMaxAI/MiniMax-H3 \
  --model-variant fl2va \
  --minimax-h3-adaln-cache-path /models/minimax-h3-fl2va-adaln-50step.safetensors \
  --num-gpus 4 \
  --tp-size 2 \
  --ulysses-degree 2 \
  --port 30010
```

`$TRANSFORMER_PATH` is the `FL2VA/transformer` or `Ref2VA/transformer`
directory in the normal SGLang/Hugging Face snapshot; the builder never
downloads a second copy. A cache only covers the scheduler settings used to
create it, including its mode, step count, flow shifts, and condition noise
values. SGLang rejects a request outside that coverage instead of silently
changing conditioning. Cache mode supports the matching unquantized checkpoint
only.

### Advanced: online AdaLN rebuild with a host cache

`--minimax-h3-adaln-online true` needs no prebuilt artifact: the server drops
the 24.2 GiB of `adaln_proj` weights from the GPU and computes each request's
AdaLN outputs from the checkpoint on demand, bit-exact with the resident-weight
path. Because a rebuild pass streams the whole 24.2 GiB, the first request of
every new `(task shape, num_inference_steps, flow_shift, audio_flow_shift)`
combination pays several seconds; after that the plans are served from a
64-slot GPU slab (LRU per plan) backed by a pinned host cache
(`--minimax-h3-adaln-host-cache-gb`, LRU per schedule, default 8 GB per
rank), so mixed-schedule serving does not re-read the checkpoint. Plan sets
that exceed the host budget are simply recomputed on their next occurrence.
Expert escape hatches live in environment variables:
`SGLANG_DIFFUSION_MINIMAX_H3_ADALN_GPU_PLANS` resizes the GPU slab (needed
only beyond 65 inference steps) and `SGLANG_DIFFUSION_MINIMAX_H3_ADALN_FP32`
computes the one-time projections in fp32 (experimental; not bit-comparable
to resident weights, validate end-to-end before production). LoRA adapters
that modify `adaln_proj` are rejected in both cache modes rather than
silently ignored.

Both cache modes hold values derived from `adaln_proj`, so a runtime weight
update is only accepted when the cache can follow it: online mode takes a disk
update whose target directory carries native `adaln_proj` safetensors, and
rejects everything else (tensor updates, and directories without those
tensors) before a single weight is written. A sidecar is built offline and
cannot be regenerated in the server, so weight updates are rejected outright;
rebuild the sidecar against the new weights and restart.

### Serve MiniMax-H3 on Ascend NPUs

For Ascend NPU, follow the
[NPU installation guide](/docs/hardware-platforms/ascend-npus/getting-started/installation)
before starting the server.

The Ascend commands below explicitly enable the Cache-DiT configuration used
for the reported performance measurements. Remove these `SGLANG_CACHE_DIT_*`
variables to use lossless denoising. See the **Ascend NPU topology comparison**
in the Benchmarks section for the measured eight- and four-NPU latency.

The measured latency configuration also passes `--dit-cpu-offload false` to
keep the transformer resident on the NPUs. Omit this flag when lower device
memory usage is more important than avoiding CPU-to-NPU transfer latency.

For an eight-NPU host, the validated topology is TP2 + SP4 with Laser
Attention. Use Ascend Flash Attention by replacing `laser_attn` with `fa`.

```bash 8-NPU theme={null}
SGLANG_CACHE_DIT_ENABLED=true \
SGLANG_CACHE_DIT_FN=2 \
SGLANG_CACHE_DIT_BN=1 \
SGLANG_CACHE_DIT_WARMUP=4 \
SGLANG_CACHE_DIT_RDT=0.4 \
SGLANG_CACHE_DIT_MC=4 \
SGLANG_CACHE_DIT_TAYLORSEER=true \
SGLANG_CACHE_DIT_TS_ORDER=2 \
HCCL_BUFFSIZE=256 sglang serve \
  --model-path MiniMaxAI/MiniMax-H3 \
  --model-type diffusion \
  --model-variant fl2va \
  --dit-cpu-offload false \
  --num-gpus 8 \
  --tp-size 2 \
  --sp-degree 4 \
  --attention-backend laser_attn \
  --port 30088 \
  --component-residency text_encoder=layerwise-offload
```

For a four-NPU host, use TP2 + SP2:

```bash 4-NPU theme={null}
SGLANG_CACHE_DIT_ENABLED=true \
SGLANG_CACHE_DIT_FN=2 \
SGLANG_CACHE_DIT_BN=1 \
SGLANG_CACHE_DIT_WARMUP=4 \
SGLANG_CACHE_DIT_RDT=0.4 \
SGLANG_CACHE_DIT_MC=4 \
SGLANG_CACHE_DIT_TAYLORSEER=true \
SGLANG_CACHE_DIT_TS_ORDER=2 \
HCCL_BUFFSIZE=256 sglang serve \
  --model-path MiniMaxAI/MiniMax-H3 \
  --model-type diffusion \
  --model-variant fl2va \
  --dit-cpu-offload false \
  --num-gpus 4 \
  --tp-size 2 \
  --sp-degree 2 \
  --attention-backend laser_attn \
  --port 30088 \
  --component-residency text_encoder=layerwise-offload
```

## 4. Generate video and audio

MiniMax-H3 uses the asynchronous OpenAI-compatible video endpoint. Choose a
generation mode below, submit a job, poll its status, and then download the
completed MP4.

<Tabs>
  <Tab title="T2VA">
    MiniMax-H3 supports output durations from 4 through 15 seconds, inclusive. The
    following request keeps the verified 5-second profile at a 768-pixel short
    edge. MiniMax-H3 resolves the aligned output canvas and frame count from
    `target`.

    ```bash Command theme={null}
    video_id=$(
      curl -sS -X POST http://127.0.0.1:30010/v1/videos \
        -H "Content-Type: application/json" \
        -d '{
          "model": "MiniMaxAI/MiniMax-H3",
          "prompt": "At night, while their owner sleeps in a bedroom, three cats march in loudly playing tiny brass instruments, then abruptly file out.",
          "seconds": 5,
          "task": "t2va",
          "conditions": [],
          "target": {
            "short_edge": 768,
            "aspect_ratio": "16:9",
            "duration_seconds": 5.0
          },
          "num_outputs_per_prompt": 1,
          "num_inference_steps": 50,
          "flow_shift": 12.0,
          "audio_flow_shift": 3.0,
          "seed": 1101
        }' |
        jq -r '.id'
    )

    while true; do
      status=$(curl -sS "http://127.0.0.1:30010/v1/videos/${video_id}" | jq -r '.status')
      [ "$status" = "completed" ] && break
      [ "$status" = "failed" ] && exit 1
      sleep 1
    done

    curl -sS -L "http://127.0.0.1:30010/v1/videos/${video_id}/content" \
      -o minimax-h3-t2va.mp4
    ```

    The output contract is an MP4 containing H.264 video at 24 fps and one AAC stereo audio stream at 32 kHz.
  </Tab>

  <Tab title="FL2VA">
    For `fl2va`, provide one or two image conditions with role `keyframe`. The supported frame-index sets are `[0]`, `[-1]`, and `[0, -1]`.

    The following request uses one server-local first frame. Use
    `frame_index: -1` for a last frame, or include both entries for first-and-last
    conditioning.

    Choose FL2VA when the supplied image should be the actual first or last frame
    of the generated clip. Use image-based Ref2VA instead when the image should
    guide identity, style, or composition without being preserved as an endpoint;
    Ref2VA may recompose or crop the reference.

    ```bash Command theme={null}
    curl -sS -X POST http://127.0.0.1:30010/v1/videos \
      -H "Content-Type: application/json" \
      -d '{
        "model": "MiniMaxAI/MiniMax-H3",
        "prompt": "The supplied frame continues with calm, natural motion and synchronized ambient sound.",
        "seconds": 5,
        "task": "fl2va",
        "conditions": [
          {
            "type": "image",
            "uri": "file:///data/minimax-h3/first-frame.png",
            "role": "keyframe",
            "frame_index": 0
          }
        ],
        "target": {
          "short_edge": 768,
          "aspect_ratio": "auto",
          "duration_seconds": 5.0
        },
        "num_outputs_per_prompt": 1,
        "num_inference_steps": 50,
        "flow_shift": 12.0,
        "audio_flow_shift": 3.0,
        "seed": 2101
      }'
    ```
  </Tab>

  <Tab title="V2V">
    V2V uses the reference-conditioning weights. Launch the server with
    `--model-variant ref2va`, keep the request `task` set to `ref2va`, and provide a video
    reference in `conditions`. There is no separate `v2v` task value.

    Use `type: "video"` when the input may be silent. If the file has a soundtrack,
    H3 also uses it as an audio reference. Use `type: "video_audio"` only when both
    streams are required; that form rejects an input without audio. The prompt tag
    for the visual stream is `<Video 1>`; an available soundtrack is exposed as
    `<Audio 1>`.

    <Note>
      Ref2VA treats the input video as reference material, not as a pixel-aligned
      edit source. It can resynthesize or reorder motion and cuts, and it does not
      expose a denoising-strength control. Do not rely on it to preserve every source
      frame or exact timing.
    </Note>

    Set `conditions[].start_time_seconds` to select a segment from a longer source.
    The default is `0`. SGLang seeks the visual stream and soundtrack to the same
    offset, then decodes at most the requested target duration in one pass; the
    source is not re-encoded into an intermediate clip.

    ```bash Command theme={null}
    curl -sS -X POST http://127.0.0.1:30010/v1/videos \
      -H "Content-Type: application/json" \
      -d '{
        "model": "MiniMaxAI/MiniMax-H3",
        "prompt": "Follow the motion and appearance of <Video 1>, changing the setting to a moonlit bedroom while preserving coherent timing.",
        "seconds": 5,
        "task": "ref2va",
        "conditions": [
          {
            "type": "video",
            "uri": "file:///data/minimax-h3/input.mp4",
            "role": "reference",
            "start_time_seconds": 35.0
          }
        ],
        "target": {
          "short_edge": 768,
          "aspect_ratio": "16:9",
          "duration_seconds": 5.0
        },
        "num_outputs_per_prompt": 1,
        "num_inference_steps": 50,
        "flow_shift": 12.0,
        "audio_flow_shift": 3.0,
        "seed": 4101
      }'
    ```

    Use `conditions[].uri` for H3 V2V. The generic top-level `video_path`,
    `video_url`, and `video_reference` upload fields are not lowered into H3
    reference conditions.
  </Tab>

  <Tab title="Multimodal Ref2VA">
    For `ref2va`, first launch the reference-conditioning capability with
    `--model-variant ref2va`, then provide conditions with role `reference`.
    Image, video, and audio references can be combined. Material tags in the
    prompt use the one-based order for each modality.

    An image condition here is semantic reference material rather than a
    pixel-aligned first frame. Use the FL2VA tab when animating a screenshot from
    that exact starting composition.

    ```bash Command theme={null}
    curl -sS -X POST http://127.0.0.1:30010/v1/videos \
      -H "Content-Type: application/json" \
      -d '{
        "model": "MiniMaxAI/MiniMax-H3",
        "prompt": "Use <Picture 1> as the visual subject and <Audio 1> as the sound reference, with coherent natural motion.",
        "seconds": 5,
        "task": "ref2va",
        "conditions": [
          {
            "type": "image",
            "uri": "file:///data/minimax-h3/reference.png",
            "role": "reference"
          },
          {
            "type": "audio",
            "uri": "file:///data/minimax-h3/reference.mp3",
            "role": "reference"
          }
        ],
        "target": {
          "short_edge": 768,
          "aspect_ratio": "auto",
          "duration_seconds": 5.0
        },
        "num_outputs_per_prompt": 1,
        "num_inference_steps": 50,
        "flow_shift": 12.0,
        "audio_flow_shift": 3.0,
        "seed": 3101
      }'
    ```
  </Tab>
</Tabs>

Poll and download any conditioned request with the same job-status and
content endpoints used in the T2VA example. Server-local `file://` URIs must
refer to files visible inside the SGLang server environment.

## 5. LoRA recipes

H3 accepts both native fused adapters and standard Diffusers/PEFT adapters.
Native adapters target modules such as `blocks.*.attn.qkv_proj`; PEFT adapters
may instead provide separate `to_q`, `to_k`, and `to_v` projections and the
`default` adapter namespace. SGLang normalizes both layouts.

The following pinned FL2VA adapters have distinct purposes:

| Recipe | Repository and pinned file | Request setting | Prompt requirement |
| - | - | - | - |
| Recommended speed/quality balance | [`larryvrh/MiniMax-H3-Turbo-Lora`](https://huggingface.co/larryvrh/MiniMax-H3-Turbo-Lora), `minimax_h3_turbo_v4_step600_ema.safetensors` | `num_inference_steps: 9` (8 denoiser evaluations), `lora_scale: 1.0` | None |
| Most aggressive speed preset (standard PEFT layout) | [`lightx2v/Minimax-h3-Turbo`](https://huggingface.co/lightx2v/Minimax-h3-Turbo), `minimax_h3_fl2v_turbo_4step_v0.1.safetensors` | `num_inference_steps: 5` (4 denoiser evaluations), `lora_scale: 1.0`, `lora_alpha: 8` | None |
| Realistic people style | [`fal/MiniMax-H3-Realism-People-LoRA`](https://huggingface.co/fal/MiniMax-H3-Realism-People-LoRA), `h3-realism-people-t2v-i2v-r2v.safetensors` | Keep the normal `num_inference_steps: 50` schedule; start with `lora_scale: 0.7` | Include `r34l1sm` in the prompt |

The H3 request field controls the number of sigma grid points, including the
terminal zero; the denoising loop therefore runs one fewer model evaluation.
This is why an adapter described as 8-step uses `9`, and a 4-step adapter uses
`5`, in the request.

All three use the same launch shape. Pinning the filename is required for
repositories that publish multiple revisions, and is also recommended for a
reproducible single-file recipe:

```bash Command theme={null}
LORA_REPO=larryvrh/MiniMax-H3-Turbo-Lora
LORA_FILE=minimax_h3_turbo_v4_step600_ema.safetensors
LORA_NAME=h3-turbo-v4
LORA_SCALE=1.0
LORA_ALPHA_ARGS=()
# LightX2V only: LORA_ALPHA_ARGS=(--lora-alpha 8)

sglang serve \
  --model-path MiniMaxAI/MiniMax-H3 \
  --model-variant fl2va \
  --num-gpus 4 \
  --ulysses-degree 4 \
  --performance-mode speed \
  --lora-path "$LORA_REPO" \
  --lora-weight-name "$LORA_FILE" \
  --lora-nickname "$LORA_NAME" \
  --lora-scale "$LORA_SCALE" \
  "${LORA_ALPHA_ARGS[@]}" \
  --lora-merge-mode auto \
  --port 30010
```

`auto` merges an adapter into ordinary resident weights to avoid per-step LoRA
matmuls, but keeps the dynamic path for FSDP-sharded weights where a full
gather can increase peak memory. Use `dynamic` when one resident server must
switch repeatedly between base and LoRA output.

Use the filename, scale, and request schedule from the table together. The
4-evaluation LightX2V recipe is the more aggressive latency/quality tradeoff.
Its checkpoint has rank 128 but omits the training alpha from both the file and
repository metadata, so `--lora-alpha 8` is required to reproduce the author's
reference implementation. Start with the Larry 8-evaluation recipe when
preserving fine visual detail is more important than minimum latency.

The pinned files above were trained for the **FL2VA** partition and apply to
`t2va` or `fl2va` requests. Some repositories, including LightX2V, publish
separate files for Ref2VA/Ref2V; select one explicitly for a `ref2va` server
rather than reusing an FL2VA file. Those Ref2VA files are not yet a pinned,
validated recipe on this page. Also avoid stacking a distilled adapter with
`quality: "high"`: both alter denoising, and that combination has not been
quality-validated.

AdaLN-pruned Diffusers components that publish `adaln_basis` and `adaln_mean`
can also consume a LoRA trained against the released full-width AdaLN modules:
SGLang projects those adapter factors onto the pruned coordinates at load time.
A structurally modified checkpoint without that metadata still fails closed,
and packed GGUF weights remain incompatible with LoRA.

### PDD acceleration LoRAs

[Alibaba PAI's MiniMax-H3-Acc-LoRAs](https://huggingface.co/alibaba-pai/MiniMax-H3-Acc-LoRAs)
use Parallel Decoding Distillation (PDD) for eight denoiser evaluations. These
checkpoints contain backbone LoRA weights and interval-specific output heads.
Merge the backbone weights offline, then fuse the output heads before serving.
The `--lora-path` recipe above does not perform these preparation steps.

Match the adapter and original MiniMax-H3 transformer weights to the server's
model variant:

| Model variant | Adapter file | Tasks |
| - | - | - |
| `fl2va` | `MiniMax-H3-FL2VA-Acc-8Step.safetensors` | `t2va`, `fl2va` |
| `ref2va` | `MiniMax-H3-Ref2VA-Acc-8Step.safetensors` | `ref2va` |

`BASE_TRANSFORMER_DIR` is the directory containing the original model's
BF16/FP32 transformer weight files, not the model root or the LoRA directory.
If you downloaded `MiniMaxAI/MiniMax-H3` to `/path/to/MiniMax-H3`, use
`/path/to/MiniMax-H3/FL2VA/transformer` for T2VA/FL2VA or
`/path/to/MiniMax-H3/Ref2VA/transformer` for Ref2VA. This directory contains
`model-*.safetensors`, `model.safetensors.index.json`, and `config.json`.
The builder reads these files and writes the merged weights to
`$PDD_DIR/transformer`, leaving the original weights unchanged.

The following example prepares FL2VA
weights and serves them on eight NVIDIA CUDA GPUs with TP2 + Ulysses4:

```bash Command theme={null}
BASE_TRANSFORMER_DIR=/path/to/MiniMax-H3/FL2VA/transformer
LORA_DIR=/path/to/h3-acc-loras
PDD_DIR=/path/to/h3-fl2va-pdd

hf download alibaba-pai/MiniMax-H3-Acc-LoRAs \
  MiniMax-H3-FL2VA-Acc-8Step.safetensors --local-dir "$LORA_DIR"

python3 -m sglang.multimodal_gen.tools.build_minimax_h3_pdd_weights \
  "$BASE_TRANSFORMER_DIR" \
  "$LORA_DIR/MiniMax-H3-FL2VA-Acc-8Step.safetensors" \
  "$PDD_DIR"

python3 -m sglang.multimodal_gen.tools.fuse_minimax_h3_pdd_heads \
  "$PDD_DIR" --video-shift 12.0 --audio-shift 3.0

SGLANG_DIFFUSION_MINIMAX_H3_PDD_HEADS="$PDD_DIR/pdd_fused_heads.safetensors" \
sglang serve \
  --model-path MiniMaxAI/MiniMax-H3 \
  --model-variant fl2va \
  --transformer-weights-path "$PDD_DIR/transformer" \
  --num-gpus 8 \
  --tp-size 2 \
  --ulysses-degree 4 \
  --performance-mode speed \
  --port 30010
```

Keep `pdd_config.json` beside `pdd_fused_heads.safetensors`, outside the
`transformer/` directory. For Ref2VA, use its adapter and base transformer,
a separate output directory, and `--model-variant ref2va`.

Use the requests in [Generate video and audio](#4-generate-video-and-audio)
with `num_inference_steps: 9`, `flow_shift: 12.0`, and `audio_flow_shift: 3.0`.
The nine sigma grid points give eight denoiser evaluations. These settings
must match the fused heads; the server rejects a different step count or
shift. Use `quality: "lossless"` in requests.

## 6. FastH3: 4-step distilled preview

[FastVideo/FastVideo-FastH3-4-step-Preview-v1-VSA-DataFree](https://huggingface.co/FastVideo/FastVideo-FastH3-4-step-Preview-v1-VSA-DataFree)
is a 4-step DMD2 distillation of MiniMax-H3, trained data-free with Video
Sparse Attention (VSA) at 0.9 sparsity and 64-token tiles. Only the T2VA
capability was distilled: requests must use `task: "t2va"`, and `fl2va` /
`ref2va` requests are rejected. The checkpoint inherits the MiniMax-H3
Community License.

Pass the repository directly to `--model-path`. The flat native-Diffusers
upload is materialized into the base-H3 layout through a registered model
overlay; the only non-symlink step is a one-time re-serialization of the
roughly 10 GB video VAE on first launch.

```bash 4×B300 VSA-H3 theme={null}
sglang serve \
  --model-path FastVideo/FastVideo-FastH3-4-step-Preview-v1-VSA-DataFree \
  --num-gpus 4 \
  --attention-backend video_sparse_attn_h3 \
  --attention-backend-config '{"VSA_sparsity": 0.9}' \
  --port 30010
```

Requests use the same asynchronous video endpoint as the base model, with
`task: "t2va"`, `conditions: []`, and a target such as
`{"short_edge": 768, "aspect_ratio": "16:9", "duration_seconds": 5.0}`. The
request default is `num_inference_steps: 5`: five points on the standard
shift-12/shift-3 sigma grid, i.e. the four distilled DiT evaluations. Any
other step count is rejected.

`video_sparse_attn_h3` (VSA-H3) is the trained sparse policy: an in-tree
Triton block-sparse kernel (SM90 / SM100 / SM103) over segment-pure prefix
tiles and (4, 4, 4) video tiles, driven by the checkpoint's trained
`to_gate_compress` compression branch. Only the DiT runs sparse; the token
refiner, text encoder, and VAEs keep their dense defaults. Ulysses sequence
parallelism is supported. See
[Attention Backends](/docs/sglang-diffusion/attention_backends) for
`VSA_sparsity`, `vsa_mode`, `vsa_dense_first_n_steps`, and
`vsa_dense_layers`. Every dense backend that runs on base H3 (`fa`,
`torch_sdpa`, ...) also runs on the FastH3 weights without VSA flags, and
`sglang generate` takes the same flags as `sglang serve`.

Measured latencies for the 4× B300 recipe are in
[FastH3 on B300](#fasth3-on-b300).

FastH3 rejects deployment options that do not apply to the distilled preview
instead of silently ignoring them: `--model-variant`, `quality: "high"`,
`fl2va` / `ref2va` requests, and, with VSA-H3, `--ring-degree` greater than 1,
`torch.compile`, and breakable CUDA graph execution.

<Warning>
  The sibling `FastVideo/FastVideo-FastH3-4-step-Preview-v1-LoRA` adapters carry
  full-rank `.diff` / `.diff_b` deltas and `set_weight` gate tensors beyond the
  LoRA contract; `--lora-path` rejects them with an explicit error. Serve the
  merged VSA-DataFree checkpoint above instead.
</Warning>

<Note>
  Upstream labels this checkpoint a preview. Quality gaps versus base H3 on hard
  motion and fine detail are properties of the released distillation, not of the
  SGLang port. Use base MiniMax-H3 when output quality matters more than
  latency.
</Note>

## 7. VDN-H3: hybrid attention, 8-step distill

[Video DeltaNet](https://openvdn.github.io/) (VDN) is a hybrid attention
architecture that approaches the speed of linear attention while maintaining
near-lossless quality. It splits the video-video attention into two
complementary branches: a sliding-window softmax branch for local frame pairs
(five consecutive latent frames form a chunk, and each chunk attends to
itself, the previous chunk, and the following chunk; the first and last
frames, text and audio stay dense) and a bidirectional linear branch, Video
Delta Attention, for long-range context.
[OpenVDN/vdn-minimax-h3](https://huggingface.co/OpenVDN/vdn-minimax-h3)
applies it to MiniMax-H3: the released `stage-dmd-step-250` checkpoint adds a
4.3 GB linear branch and OpenVDN's own 8-step DMD2 LoRA on the unchanged H3
backbone. It is the FL2VA partition, so it serves `t2va` and `fl2va` (first,
last, or both keyframes); `ref2va` is rejected. The weights inherit the
MiniMax-H3 Community License.

Pass the repository directly to `--model-path`. On first launch a registered
overlay prefuses both adapters into the transformer weights (a 62 GB write)
and attaches the linear branch; the Qwen3-VL conditioner and the VAEs are
hard-linked from `MiniMaxAI/MiniMax-H3`. Keep `SGLANG_DIFFUSION_CACHE_ROOT` on
the same filesystem as the Hugging Face cache with at least 90 GB free.

```bash 8×B200 theme={null}
sglang serve \
  --model-path OpenVDN/vdn-minimax-h3 \
  --num-gpus 8 \
  --attention-backend hybrid_window_attn_h3 \
  --performance-mode speed \
  --warmup-num-frames 345 \
  --warmup-resolutions 1344x768 \
  --port 30010
```

`--num-gpus 4`, `2` or `1` gives the smaller Ulysses recipes in the benchmark
table; `sglang generate` takes the same flags. `--warmup-num-frames` /
`--warmup-resolutions` warm up at the clip length and canvas you will serve;
without them the first long request pays 2 to 3 s of allocator growth and
kernel setup.

Requests use the same video endpoint as the base model with `task: "t2va"`
and `conditions: []`, or `task: "fl2va"` with the
[keyframe conditions](#4-generate-video-and-audio), and a target such as
`{"short_edge": 768, "aspect_ratio": "16:9", "duration_seconds": 14.375}` (the
345-frame paper workload). The default `num_inference_steps: 9` is the only
accepted value: nine sigma grid points, eight DiT forwards. Each keyframe adds
about 2,000 dense rows, so an `fl2va` forward costs a little more than `t2va`.

`hybrid_window_attn_h3` is required: a dense backend would silently skip the
linear branch and the gates and produce the wrong model. On Blackwell
(SM100 / SM103 / SM120) the transformer defaults to online MXFP8;
`--quantization bf16` opts out. Before SM100, `--quantization fp8` is base
H3's per-channel fp8 path, and SM80 runs the bf16 DiT (62 GB; budget for
`--layerwise-offload-components`). Ampere and Ada are enabled but not
benchmarked. Ulysses sequence parallelism is supported; `--model-variant`,
`quality: "high"`, `--ring-degree` greater than 1, `torch.compile`, and
breakable CUDA graph execution are rejected. The backend's
`--attention-backend-config` keys are listed in
[Attention Backends](/docs/sglang-diffusion/attention_backends).

Measured latencies for 1, 2, 4 and 8 B200s are in
[VDN-H3 on B200](#vdn-h3-on-b200); single-card and PCIe multi-card numbers
for the RTX PRO 6000 are in [VDN-H3 on RTX PRO 6000](#vdn-h3-on-rtx-pro-6000).

## 8. Sampling and output controls

MiniMax-H3 supports more than one output per prompt. The video API accepts
`num_outputs_per_prompt` (or OpenAI-compatible `n`) from 1 through 10. Offline
generation accepts `--num-outputs-per-prompt N`; `--num-outputs N` is the short
alias. A scalar seed is expanded deterministically as `seed + output_index`, so
the outputs do not reuse the same noise.

Same-prompt fan-out reuses text conditioning. On the verified 2× RTX 5090
recipe, a 5-step two-output request completed in 155.39 seconds versus 78.11
seconds for one output, while producing two distinct valid MP4 files. The
independent denoise and decode passes remain sequential on this 32 GB profile
to keep peak memory bounded; the grouped path adds essentially no orchestration
overhead. Use server replicas when lower wall-clock latency for many variants
matters more than per-server memory efficiency.

For example, set `"num_outputs_per_prompt": 2` in any request above. After the
job completes, download both outputs by selecting each zero-based variant:

```bash Command theme={null}
video_id="<completed-job-id>"
for variant in 0 1; do
  curl -sS -L \
    "http://127.0.0.1:30010/v1/videos/${video_id}/content?variant=${variant}" \
    -o "minimax-h3-${variant}.mp4"
done
```

### Choose the quality level

`quality` is a cumulative request-scoped optimization parameter with three
levels:

* `"lossless"` (default): the exact reference path. Output is bit-exact
  against the reference implementation and the CI ground truth.
* `"extra-high"`: includes the global fusion-only tier but does not enable
  Cache-DiT or another approximate optimization. MiniMax-H3 currently has no
  request-gated fusion site, so its denoise path is the same as `lossless`.
* `"high"`: the audited accelerated path. Quality is guaranteed (the audited
  Cache-DiT configuration measures SSIM 0.931 / PSNR 28.16 dB against
  `lossless`), but output is no longer bit-identical to the reference.

One resident server serves all three levels; a `quality: "high"` request
mounts its audited Cache-DiT policy at the batch boundary, and a later
`quality: "lossless"` or `quality: "extra-high"` request removes the hooks
before denoising.

Start the validated server once:

```bash Command theme={null}
sglang serve \
  --model-path MiniMaxAI/MiniMax-H3 \
  --model-variant fl2va \
  --num-gpus 4 \
  --tp-size 1 \
  --sp-degree 4 \
  --ulysses-degree 4 \
  --ring-degree 1 \
  --encoder-parallel auto \
  --performance-mode speed \
  --use-fsdp-inference false \
  --enable-torch-compile false \
  --port 30010
```

Then choose a request level:

<Tabs>
  <Tab title="lossless (default)">
    Native denoising with no feature-cache approximation. This is the default;
    omitting the field is equivalent.

    ```json Request field theme={null}
    {
      "quality": "lossless"
    }
    ```
  </Tab>

  <Tab title="extra-high">
    The global fusion-only tier. It does not enable MiniMax-H3 Cache-DiT and
    currently follows the same H3 denoise path as `lossless`.

    ```json Request field theme={null}
    {
      "quality": "extra-high"
    }
    ```
  </Tab>

  <Tab title="high">
    The audited accelerated path. Use it when you can trade bit-exactness for
    latency while keeping output closest to the same-seed lossless trajectory.

    ```json Request field theme={null}
    {
      "quality": "high"
    }
    ```
  </Tab>
</Tabs>

The measured trade-off is:

| `quality` | Mean <br />inference <br />latency | Speedup | SSIM vs <br />lossless | PSNR vs <br />lossless | Expected <br />trade-off |
| - | -: | -: | -: | -: | - |
| `lossless` | 75.10 s | 1.00× | 1.000 | exact | Native reference path |
| `extra-high` | Not separately measured | — | Same H3 denoise path | Same H3 denoise path | Fusion-only tier; no H3-specific request-gated site yet |
| `high` | 53.70 s | 1.40× | 0.931 | 28.16 dB | Smallest same-seed visual change |

These numbers use 1344×768, 124-frame, 24 fps T2VA with 50 inference steps,
video flow shift 12, audio flow shift 3, and three fixed prompt/seed pairs on
4×H200. The prompts cover a quiet detailed scene, fast multi-subject action,
and a moving close-up portrait. `inference_time_s` is averaged across the three
prompts; the quiet-scene point is itself the mean of two repeats.

SSIM and PSNR compare decoded, frame-aligned output with the `lossless`
result for the same prompt and seed. They measure trajectory deviation, not
absolute perceptual quality: the `high` path can produce a different but
still plausible realization. It also changes the joint audio-video denoise
trajectory, while these two metrics cover video only.

`quality: "high"` currently accepts only the exact workload and 4×H200
deployment above; other hardware, task modes, request shapes, step counts, or
flow shifts fail before denoising. Offline generation uses the same level
name, for example `sglang generate --quality high`.

<Note>
  `quality` selects a model sampling level and can change generated content.
  `output_quality` controls only output-file compression; it is a separate field.
</Note>

For manually tuned Cache-DiT experiments outside that validated path, omit
the request `quality` field and set `--enable-cache-dit` or the
process-wide `SGLANG_CACHE_DIT_*` defaults. Any explicit `quality`, including
`"lossless"` and `"extra-high"`, takes H3 off the generic Cache-DiT path. The
24 GB layerwise recipe above can use the same switch; skipped blocks are
not streamed.

```bash Command theme={null}
SGLANG_CACHE_DIT_ENABLED=true \
SGLANG_CACHE_DIT_FN=1 \
SGLANG_CACHE_DIT_BN=0 \
SGLANG_CACHE_DIT_WARMUP=4 \
SGLANG_CACHE_DIT_RDT=0.12 \
SGLANG_CACHE_DIT_MC=2 \
sglang serve \
  --model-path MiniMaxAI/MiniMax-H3 \
  --model-variant ref2va \
  --num-gpus 8 \
  --ulysses-degree 8 \
  --encoder-parallel auto \
  --performance-mode speed \
  --port 30010
```

<Warning>
  Cache-DiT skips selected block computation and is approximate. It cannot
  be combined with FSDP inference. DiT layerwise offload is compatible:
  skipped blocks are not streamed, and the first layer after a skip may
  sync-load. Breakable CUDA graph execution takes precedence and leaves
  Cache-DiT disabled. Tune the cache thresholds only after comparing both
  video and audio quality on the target task profile. A real B200 request
  has completed, but the `quality: "high"` path above remains fail-closed
  to the audited 4×H200 workload.
</Warning>

## 9. Feature contracts and advanced recipes

The generated command already contains the recommended topology and encoder
setting. Use the detailed reference below only when applying an optional
override or checking its installation, topology limits, and validation evidence.

<Tabs>
  <Tab title="Lossless runtime">
    The recommended `speed` launch already combines resident components with
    Ulysses sequence parallelism. Validation status below applies only to the
    listed hardware and topology; it is not inherited by a similar GPU family.

    | Feature | Validation status | Notes |
    | - | - | - |
    | Ulysses sequence parallelism | Verified: 8× B200, 4× H200, 4× H100, and Ulysses1/2/4/8 on MI300X and MI355X | Use `--ulysses-degree`. Combine with Ring for cross-node scaling; see the next row. |
    | Ring sequence parallelism (cross-node) | Verified: 2 nodes of 8× H200 each (Ulysses8 × Ring2) | Use `--ring-degree` together with `--nnodes`/`--node-rank`/`--dist-init-addr`. Ring shards the sequence across nodes while Ulysses shards heads within a node; H3's packed multi-segment attention only supports Ring across the node boundary, not within a single node's Ulysses group. Requires `--encoder-parallel replicate` — `auto`'s fold decision is not node-boundary aware. See the benchmark section below. |
    | Tensor parallelism | Verified: B200 TP2 + Ulysses4; H100 TP2 + Ulysses2 and TP4 + Ulysses1 | `--tp-size` may be combined with Ulysses when the TP-local head count remains divisible by the Ulysses degree. On 4×H100, TP2 + Ulysses2 is the measured speed default. |
    | FSDP inference | Verified: 4× B200 and 4× H100 + Ulysses4 | Preserves H3's mixed BF16/FP32 parameter policy. B200 completed the exact eager comparison; H100 completed consecutive real requests at about 57 GB peak memory per GPU. |
    | Resident components | Verified: B200, H200, 4×H100 with TP, and 1/2/4/8× MI300X and MI355X | This is the recommended single-request latency path when the complete workload fits. |
    | CPU and layerwise offload | Verified: 2× RTX 5090 TP2; 1× RTX 4090 24 GB | The 5090 lossless recipe keeps 20 DiT blocks plus both VAE encoders resident, streams the remaining DiT blocks, text encoder, and video VAE decoder blocks, and leaves the small audio VAE resident. The 4090 recipe streams DiT and the text encoder with zero resident DiT layers and **omits `vae`** from `--layerwise-offload-components`. Compatible with Cache-DiT; skipped blocks are not streamed. |
    | Breakable CUDA graph | Verified: B200 Ref2VA, opt-in | Matching eager output was observed for the captured signature, without a measured speedup. Re-capture for other shapes and reference sets. |
    | `torch.compile` | Measured: H200, opt-in | Steady-state benefit was below measurement noise, while startup increased and numerical output changed. Do not use it for consistency ground truth. |

    The verified parallel, placement, and matching-signature BCG paths keep the
    BF16/FP32 weights and denoising math. `torch.compile` is the exception called
    out above. Always use the eager BF16/FP32 launch when producing CI consistency
    ground truth.

    For the validated 1344×768 Ref2VA profile, use a 5504-row text bucket so both
    the server warmup and reference-conditioned requests share the captured
    signature:

    ```bash Command theme={null}
    sglang serve \
      --model-path MiniMaxAI/MiniMax-H3 \
      --model-variant ref2va \
      --num-gpus 8 \
      --ulysses-degree 8 \
      --encoder-parallel auto \
      --performance-mode speed \
      --enable-breakable-cuda-graph true \
      --warmup-resolutions 1344x768 \
      --bcg-text-buckets 5504 \
      --port 30010
    ```

    BCG is lossless for a matching captured signature, but capture reserves extra
    GPU memory. Re-measure the live H3 text length before reusing this bucket for a
    different task profile, reference set, resolution, or prompt template.
  </Tab>

  <Tab title="Attention backends">
    Leave `--attention-backend` unset for the platform default. Use
    `--attention-backend fa` only for an explicit FlashAttention comparison.

    SageAttention uses quantized attention math and is not a consistency mode. To
    select H3's native packed-varlen Sage path, install the dependency and add
    `--attention-backend sage_attn`. On Hopper, install the upstream SM90 binding
    fix rather than the PyPI 2.2.0 build:

    ```bash Command theme={null}
    pip install --force-reinstall \
      git+https://github.com/thu-ml/SageAttention.git@d9704247a5139ab4c03bf7fc6b35cc0e2cbb5ea4 \
      --no-build-isolation
    ```

    The backend is a server-wide default. Use
    `--component-attention-backends` only when a measured component needs a
    different kernel, and keep the platform default for every component not named
    in the override.
  </Tab>

  <Tab title="Online quantization">
    On the verified 8× B200 topology, quantize the BF16 transformer at server load:

    ```bash Command theme={null}
    sglang serve \
      --model-path MiniMaxAI/MiniMax-H3 \
      --model-variant ref2va \
      --num-gpus 8 \
      --ulysses-degree 8 \
      --encoder-parallel auto \
      --performance-mode speed \
      --quantization fp8 \
      --port 30010
    ```

    H3 automatically keeps its video/audio patch projections, timestep MLP, and
    final video/audio heads in FP32. All other linear layers have stable full
    module prefixes, so additional layers can be kept unquantized:

    ```bash Command theme={null}
    sglang serve \
      --model-path MiniMaxAI/MiniMax-H3 \
      --model-variant ref2va \
      --num-gpus 8 \
      --ulysses-degree 8 \
      --encoder-parallel auto \
      --quantization fp8 \
      --quantization-ignored-layers blocks.0.attn token_refiner \
      --port 30010
    ```

    <Warning>
      Online FP8 is approximate and is not a consistency ground-truth mode. It can
      be combined with Cache-DiT, but the two approximations compound. Validate
      visual quality, audio quality, memory use, and latency on the target workload.
      This recipe is limited to the resident B200 and B300 topologies used for real
      H3 validation runs.
    </Warning>

    On a single 24 GB card, use `kitchen_int8` instead of FP8. It quantizes the
    four GEMMs per DiT block online from the Hub BF16 weights (data-free, no
    calibration) and dispatches them through `comfy_kitchen.int8_linear`.
    Quantization happens after H3's grouped `qkv` reorder, so do not load an
    externally pre-quantized INT8 checkpoint here.

    ```bash Command theme={null}
    sglang generate \
      --model-path MiniMaxAI/MiniMax-H3 \
      --model-variant fl2va \
      --quantization kitchen_int8 \
      --attention-backend fa \
      --performance-mode memory \
      --layerwise-offload-components dit,text_encoder \
      --dit-offload-prefetch-size 1 \
      --dit-layerwise-resident-layers 0 \
      --enable-torch-compile false \
      --prompt "A cat walking on a sunny beach, gentle waves." \
      --save-output
    ```

    `fa` keeps exact attention. For a faster, approximate DiT path, use
    `--attention-backend sol_attn` with
    `--attention-backend-config dense_backend=sage_attn,dense_steps=10` and
    `--component-attention-backends text_encoder=torch_sdpa,transformer=sol_attn`.
    See [Quantization](/docs/sglang-diffusion/quantization#kitchen-int8)
    and [Attention Backends](/docs/sglang-diffusion/attention_backends#sage-then-sol-hybrid).

    <Warning>
      `kitchen_int8` changes Linear numerics. `sol_attn` / `sage_attn` also change
      the attention algorithm. Neither is a consistency ground-truth mode. The
      BF16 path is unchanged when `comfy-kitchen` is not installed.
    </Warning>

    Pre-quantized and compact H3 text encoders are listed once in
    [Checkpoint and adapter formats](#checkpoint-and-adapter-formats). They use
    component-local paths and never inherit the DiT's `--quantization` setting.
    SGLang reads their metadata before constructing the native Qwen3-VL encoder and
    fails closed when the selected format, projection, or topology is incompatible.
  </Tab>

  <Tab title="Cube sparse attention">
    Cube sparse attention applies TopK sparsity only to H3's 3D visual streams.
    Text, audio, standalone reference images, and the text-only token refiner stay
    dense. It runs on pure PyTorch plus FlexAttention, so it has no third-party
    kernel dependency.

    Select it for the H3 transformer with
    `--component-attention-backends transformer=cube_sparse_attn` and pass
    `--attention-backend-config` with both `local_cube_size` and
    `topk_ratio_list`. Scoping the backend leaves the text encoder on its native
    backend:

    ```bash Command theme={null}
    sglang serve \
      --model-path MiniMaxAI/MiniMax-H3 \
      --model-variant ref2va \
      --num-gpus 8 \
      --ulysses-degree 8 \
      --performance-mode speed \
      --component-attention-backends transformer=cube_sparse_attn \
      --attention-backend-config '{"local_cube_size": [4, 4, 4], "topk_ratio_list": [1.0, 1.0, 0.8, 0.7, 0.6, 0.5, 0.5]}' \
      --port 30010
    ```

    * `local_cube_size` is the `(T, H, W)` cube that groups neighboring latent
      tokens into one attention block. It must have exactly three entries.
    * `topk_ratio_list` sets the per-step keep ratio and must have exactly one
      entry per denoise step, each in `(0, 1]`. A ratio of `1.0` keeps a step
      on H3's native dense attention path; smaller values select the FlexAttention
      sparse path and drop more blocks. The example above matches a request with
      `num_inference_steps: 8`, whose endpoint-inclusive sigma schedule has seven
      denoise updates.

    Cube labeling is coordinate-driven. FL2VA keyframes share the target video's
    position grid, so a keyframe token and a target token at the same `(T, H, W)`
    coordinate receive the same semantic cube label. Duplicate coordinates do not
    extend the temporal grid; a semantic cube can therefore span multiple physical
    attention blocks. In Ref2VA, standalone reference images remain dense, while
    reference videos and the target video contribute to one global TopK candidate
    pool rather than receiving separate per-stream quotas.

    <Warning>
      Cube sparse attention is an approximate backend and is not a consistency
      ground-truth mode. `topk_ratio_list` length must equal the denoise step count
      or the server rejects the request. Cube sparse attention does not support Ring
      parallelism; use `--ulysses-degree` without `--ring-degree`. FlexAttention's
      routing overhead can outweigh sparse-kernel savings on short sequences, so
      benchmark latency as well as visual and audio quality on the target workload.
    </Warning>
  </Tab>

  <Tab title="SubBlock sparse attention">
    SubBlock sparse attention applies training-free block sparsity to MiniMax-H3's
    long, non-causal DiT self-attention. The BF16 path supports CUDA SM90, SM100,
    and SM120. Other compute capabilities, including SM103/B300, are rejected.
    Ulysses sequence parallelism is supported; Ring parallelism is not.

    The global backend selects the H3 DiT lazily, so keep the Qwen text encoder on
    its compatible dense backend with a component override:

    ```bash Command theme={null}
    sglang serve \
      --model-path MiniMaxAI/MiniMax-H3 \
      --model-variant fl2va \
      --num-gpus 8 \
      --ulysses-degree 8 \
      --attention-backend subblock_sparse_attn \
      --component-attention-backends text_encoder=fa \
      --attention-backend-config '{"sparsity": 0.75, "skip_first_steps": 10}' \
      --port 30010
    ```

    Use `text_encoder=torch_sdpa` on SM120. The default configuration drops at
    most 75% of key blocks after the first 10 denoise forwards; short sequences,
    cross-attention, and unsupported shapes continue on the dense path. Tune
    `sparsity` and `skip_first_steps` together with visual and audio quality checks.

    On H100/H200 (SM90), `compute_mode=sage_fp8` switches the sparse kernel to
    online INT8 Q/K and FP8 P/V compute. Install the optional kernel and add the
    compute mode:

    ```bash Command theme={null}
    pip install git+https://github.com/thu-ml/SpargeAttn.git --no-build-isolation

    sglang serve \
      --model-path MiniMaxAI/MiniMax-H3 \
      --model-variant fl2va \
      --num-gpus 8 \
      --ulysses-degree 8 \
      --attention-backend subblock_sparse_attn \
      --component-attention-backends text_encoder=fa \
      --attention-backend-config '{"compute_mode": "sage_fp8", "sparsity": 0.75}' \
      --port 30010
    ```

    <Warning>
      Both SubBlock routing and `sage_fp8` are approximate. Use dense BF16 attention
      for consistency ground truth, and validate the selected sparsity on the target
      resolution, duration, task, and checkpoint.
    </Warning>
  </Tab>

  <Tab title="Encoder scheduling">
    The picker explicitly writes `--encoder-parallel auto` in every single-node
    recipe. At the default request batch size of one, H100/H200/B200/B300 servers
    with peer-to-peer access fold the Qwen encoder across otherwise idle Ulysses
    ranks. A pure-TP recipe keeps the encoder inside its TP group, while a
    PCIe-only host can avoid an expensive world fold. Keep `auto` unless one of
    the cases below applies.

    Encoder DP is a throughput policy for compatible request batches. It requires
    TP1 and DiT DP1, replicates the encoder weights, and does not improve a batch
    of one:

    ```bash Overlay theme={null}
    --encoder-parallel dp \
    --batching-max-size 2
    ```

    The cross-node picker recipe already uses replication because the automatic
    fold decision is not node-boundary aware:

    ```bash Overlay theme={null}
    --encoder-parallel replicate
    ```
  </Tab>
</Tabs>

## 10. Configuration notes

* MiniMax-H3 produces the canonical 24 fps output; request duration is expressed through `target.duration_seconds`.
* `target.duration_seconds` must be between 4 and 15 seconds, inclusive. The command picker defaults to the verified 5-second profile.
* Use a 768-pixel short edge for the released quality recipe. The aligned output dimensions are derived from `target.aspect_ratio`.
* `flow_shift` controls video diffusion and `audio_flow_shift` controls audio diffusion.
* V2V uses `task: "ref2va"` with a `video` or `video_audio` reference; it is served by the `Ref2VA` partition and is not a separate public task value.
* `conditions[].start_time_seconds` selects a non-negative offset for a video reference. Its visual and audio streams are always sought together.
* Ref2VA condition order is semantic and must match the one-based material tags in the prompt. For Ref2VA, `target.aspect_ratio: "auto"` resolves to the model's 16:9 fallback rather than inheriting a reference asset's geometry.
* The distilled pipeline uses a single denoising branch, so CFG parallelism does not apply. Keep it disabled: a CFG degree greater than 1 is accepted for request compatibility, but the extra ranks only recompute the same branch and do not accelerate the request. Explicitly disabling CFG, or setting its size to 1, remains a valid no-op.
* The released visual VAE quality recipe uses overlapping tiled decode. SGLang keeps that recipe by default and distributes complete tiles across the decode group; this changes scheduling, not the computation inside each tile.
* H3 rejects `--vae-config.parallel-decode-mode spatial` and `spatial_shard`: validation found output mismatches. Use the default released tiled recipe.
* Keep the default `--encoder-parallel auto`. With the server’s default `batching_max_size` of 1, single-node H100/H200/B200/B300 recipes with peer-to-peer access fold the Qwen text encoder over otherwise idle Ulysses ranks. This is separate from DiT tensor parallelism. A pure-TP recipe already shards the encoder over its TP group and does not add a world fold.
* For throughput-oriented serving, select **DP (batched throughput)**. The picker pairs `--encoder-parallel dp` with an editable `--batching-max-size` greater than 1. Encoder DP stays inside each DiT replica and composes with encoder TP: the H100 TP2 + Ulysses2 recipe has two TP-sharded encoder copies that can split a batch, while the RTX 5090 pure-TP2 recipe has one encoder copy and therefore no additional batch-DP degree. It provides no benefit for a batch of one and is not bitwise-identical to the unsplit deployment.
* Use explicit **Fold** to prioritize single-request latency and encoder memory on a measured high-bandwidth single-node topology. Use **Replicate** as the compatibility path when folding or encoder DP is unsuitable.
* `--use-fsdp-inference true` shards only the DiT. MiniMax-H3 preserves the original FP32 dtype of its patch, time, and output projections during FSDP all-gather, so this path does not trade numerical correctness for memory. On 4×H100, prefer TP2 + Ulysses2 for speed; use FSDP as an explicit capacity policy rather than assuming it is faster.
* `speed` keeps model components resident, while `auto` applies the model-aware 120 GiB residency threshold. `memory` prioritizes avoiding OOM and includes the executable VAE decoder in its default layerwise set. A measured recipe with sufficient headroom can opt into `--component-residency vae=resident`; the 2×H100 CI recipe does this because the VAE's 4.8 GiB/GPU cost avoids repeated decoder transfers during tiled decode. DiT residency and prefetch knobs remain scoped to the DiT. Use `speed` only after confirming that the complete target workload fits.
* Breakable CUDA graph execution is an explicit opt-in, not part of the recommended `speed` preset. It requires `--enable-breakable-cuda-graph`, every served size in `--warmup-resolutions`, and `--bcg-text-buckets` that cover the live H3 condition sequence. The validated 1344×768 Ref2VA recipe uses 5504; other task profiles and reference sets may need a different value. It preserves eager output for matching captured signatures, but graph capture consumes additional GPU memory and may provide little latency benefit when Ulysses attention and collectives dominate, so benchmark it on the target topology before enabling it.
* `--enable-spectrum` skips selected DiT steps. On H3 it cannot combine with Cache-DiT / `quality="high"` or SP > 1. 50-step 4090 recipe is in §3.

## 11. Benchmarks

The picker exposes resident and FSDP profiles on NVIDIA datacenter GPUs. GPU
counts are properties of the selected recipes, not a claim that every platform
requires that many GPUs. The detailed tables below report performance only for
the configurations with collected measurements:

| Hardware | Default resident recipe | Other profile or topology |
| - | - | - |
| B300 | 8× Ulysses8 resident | 8× FSDP + Ulysses8; the 8-GPU sweep is not a minimum-GPU claim. |
| B200 | 8× Ulysses8 resident | 4× FSDP + Ulysses4 |
| GB300 | 4× Ulysses4 resident (FL2VA T2VA) | 2 nodes × 4× Ulysses4×Ring2 is unverified. |
| GB200 | 4× Ulysses4 resident, derived/unverified | No performance measurements. |
| H200 | 4× Ulysses4 resident | 4× FSDP + Ulysses4; 4× TP2 + Ulysses2; 2 nodes × 8× Ulysses8×Ring2 cross-node |
| H100 | 4× TP2 + Ulysses2 resident | 4× TP4 + Ulysses1; 4× FSDP + Ulysses4 |
| Ascend NPU | 8 NPUs, TP2 + SP4, Laser Attention | 4 NPUs, TP2 + SP2, Laser Attention |
| MI300X / MI355X | 8× Ulysses8 resident | 1×, 2×, and 4× scaling runs |
| RTX 5090 | 1× layerwise offload, measured on a physical desktop | 2× TP2 + layerwise offload |
| RTX 4090 24 GB | 1× layerwise offload + `kitchen_int8` | Spectrum and approximate attention backends are opt-in |

### GB300 single-host

Four requests on **1 host × 4 NVIDIA GB300**, using Linux ARM64, SGLang main
[`15d2cbcc90fc`](https://github.com/sgl-project/sglang/commit/15d2cbcc90fc66b4d08745994cc707a45b067ffc),
PyTorch 2.13.0+cu130, and NCCL 2.29.7. The source checkout was installed with
`python3 -m pip install -e "python[diffusion]" --upgrade` inside
`lmsysorg/sglang:latest`; this is a source-validation environment, not a claim
that the image's bundled release contains that revision.

The command matches the GB300 default in the builder: Ulysses4, resident
weights, automatic encoder folding, native BF16/FP32, platform-default
Dynamic cuDNN/FA attention, and eager execution. The model was downloaded by
`sglang serve` from `MiniMaxAI/MiniMax-H3`.

The workload uses the builder's cat-band prompt, FL2VA T2VA, a 5-second request
at a 768-pixel short edge, 50 inference steps, `quality="lossless"`, one output,
and seed 1101. Every MP4 contained video and audio and passed a full decode check.

| First full request after server warmup | Following three requests | Warm median |
| -: | - | -: |
| 35.16 s | 33.11 / 33.10 / 33.10 s | **33.10 s** |

These are client-observed completion times, including up to one second of
status-polling delay. Download, model loading, and server warmup are excluded.
This is a deployment smoke/latency measurement, not a cross-framework quality
audit, a minimum-GPU claim, or an extrapolation to GB200/NVL72 scaling.

### Ascend NPU topology comparison

Both topologies used Laser Attention and the explicit Cache-DiT configuration
from the Ascend launch commands, with `--dit-cpu-offload false` keeping the DiT
resident. The measured workload was one 5-second T2VA request at 1344×768,
124 frames, 24 fps, and 50 inference steps.

| NPU count | Topology | End-to-end latency |
| -: | - | -: |
| 8 | TP2 + SP4 | **55.07 s** |
| 4 | TP2 + SP2 | **103.57 s** |

These are individual end-to-end measurements for each topology, not averages.
The eight-NPU topology had 46.8% lower end-to-end latency than the four-NPU
topology.

### B300 precision and encoder placement

A 12-configuration sweep on a single 8× B300 host, covering both checkpoint
partitions, both transformer precisions, and all three text-encoder
placements. It answers one question — *how long does one request take, and how
much memory does it need*.

### What was measured

**Hardware.** 8× NVIDIA B300 SXM6, single node.

**Model.** `MiniMaxAI/MiniMax-H3`, both released weight partitions.

**Serve command.** Exactly the recipe the picker emits for B300, plus the one
or two overlay flags under test:

```bash Command theme={null}
sglang serve \
  --model-path MiniMaxAI/MiniMax-H3 \
  --model-variant fl2va \
  --num-gpus 8 \
  --ulysses-degree 8 \
  --performance-mode speed \
  --host 0.0.0.0 \
  --port 30010
```

The swept axes are `--model-variant` (`fl2va` / `ref2va`), `--quantization`
(unset for BF16 / `fp8`), and `--encoder-parallel` (`auto` / `fold` /
`replicate`). Nothing else differs between the 12 servers.

This is a single-request latency sweep (`batching_max_size: 1`), so encoder DP
is intentionally excluded: it cannot distribute a batch of one. Use the
**DP for a request batch** setting above for a compatible multi-request
deployment; the table below does not claim a measured H3 DP speedup.

**Driver.**

```bash Command theme={null}
python3 -m sglang.multimodal_gen.benchmarks.bench_serving \
  --host 127.0.0.1 --port 30010 \
  --model MiniMaxAI/MiniMax-H3 \
  --dataset vbench --task text-to-video \
  --num-prompts 1 --max-concurrency 1 \
  --warmup-requests 1 --warmup-inference-steps 50 \
  --extra-body '{"task":"t2va","conditions":[],"target":{"short_edge":768,"aspect_ratio":"16:9","duration_seconds":5.0},"seconds":5,"flow_shift":12.0,"audio_flow_shift":3.0}'
```

**Workload**

| Property | Value |
| - | - |
| Output duration | 5.167 s |
| Resolution | 1344×768 |
| Frames | 124 @ 24 fps |
| Denoising steps | 50 |
| `flow_shift` / `audio_flow_shift` | 12.0 / 3.0 |
| Requests in flight | 1 (`--max-concurrency 1`, server at `batching_max_size: 1`) |
| Requests measured | 1 per cell, after 1 warmup request |

### Results

| Weights | Precision | Encoder | Load | Warmup | Latency | Peak/GPU |
| - | - | - | -: | -: | -: | -: |
| FL2VA | BF16 | auto | 118.1 s | 29.65 s | **19.04 s** | 83,578 MB |
| FL2VA | BF16 | fold | 114.0 s | 28.72 s | **19.04 s** | 83,578 MB |
| FL2VA | BF16 | replicate | 116.0 s | 28.33 s | **19.04 s** | 124,158 MB |
| FL2VA | FP8 | auto | 116.0 s | 27.16 s | **18.03 s** | 51,926 MB |
| FL2VA | FP8 | fold | 116.0 s | 25.99 s | **18.04 s** | 51,926 MB |
| FL2VA | FP8 | replicate | 118.0 s | 27.97 s | **18.04 s** | 92,506 MB |
| Ref2VA | BF16 | auto | 114.0 s | 38.69 s | **29.12 s** | 83,968 MB |
| Ref2VA | BF16 | fold | 118.0 s | 36.58 s | **29.13 s** | 83,968 MB |
| Ref2VA | BF16 | replicate | 116.0 s | 35.17 s | **29.13 s** | 124,490 MB |
| Ref2VA | FP8 | auto | 124.0 s | 34.30 s | **27.12 s** | 52,816 MB |
| Ref2VA | FP8 | fold | 112.0 s | 34.44 s | **27.12 s** | 52,816 MB |
| Ref2VA | FP8 | replicate | 116.0 s | 33.42 s | **27.12 s** | 93,396 MB |

### FastH3 on B300

The same 4× B300 host served [FastH3](#6-fasth3-4-step-distilled-preview)
with the VSA-H3 recipe above (1344×768 at 24 fps with audio, `task: "t2va"`,
`num_inference_steps: 5`, seed 1000, eager BF16, Ulysses4, `VSA_sparsity` 0.9).
E2E is the client wall clock of a `/v1/videos` request including decode,
muxing, and file output, median of three requests after one warm request;
the stage columns are the server timings of the same request. H3 aligns the
requested durations to 124, 243, and 362 frames. Client RTF is E2E divided by
the video duration:

| Requested / aligned | Encoder | Denoise (4 forwards) | Decode | Transport + MP4 | E2E | Client RTF | Peak/GPU |
| - | -: | -: | -: | -: | -: | -: | -: |
| 5 s / 124 | 0.07 s | 2.18 s | 0.87 s | 0.9 s | **4.1 s** | 0.79 | 95,744 MB |
| 10 s / 243 | 0.07 s | 4.83 s | 1.71 s | 1.3 s | **8.0 s** | 0.79 | 102,666 MB |
| 15 s / 362 | 0.07 s | 8.80 s | 2.56 s | 1.8 s | **13.3 s** | 0.88 | 110,774 MB |

All three requests finish faster than playback. Dense FA on the same weights
and topology takes 3.77 / 9.84 / 18.45 s (`sglang generate`, stage sum): it is
competitive at 5 s, and VSA-H3 pulls ahead from 10 s on. At 5 s,
TP2 + Ulysses2 (3.42 s, 62,290 MB), FSDP + Ulysses4 (3.42 s, 50,984 MB), and
online `--quantization fp8` (2.93 s, 64,204 MB) trade a little latency for
peak memory.

### VDN-H3 on B200

An 8× B200 (SM100, 183 GB) host served [VDN-H3](#7-vdn-h3-hybrid-attention-8-step-distill)
on the paper workload: 1344×768 at 24 fps with audio for 14.375 s (345 frames,
102 latent frames, about 104k packed rows), `task: "t2va"`,
`num_inference_steps: 9` (8 DiT forwards), seed 1000, `hybrid_window_attn_h3`,
eager, and the warmup run at the served clip shape
(`--warmup-num-frames 345 --warmup-resolutions 1344x768`), so every forward
of the served request is steady state; without those flags the first forward
pays 2 to 3 s of allocator growth and kernel setup. "Steady
s/NFE" is the mean of forwards 2 to 8. The OpenVDN reference rows ran the released
inference stack (`8nfe_tuned_fp8.yaml`, `infer_ulysses.py`) on the same host
and the same clip length, with its Ulysses layout (`parallel.softmax_ranks`)
swept at every GPU count rather than assumed. Every row below comes from one
campaign, one arm at a time on an otherwise idle node; a repeated arm moves by
about 3% across nodes, so treat smaller differences as noise:

| Config | Steady s/NFE | Denoise (8 forwards) | Decode | Peak/GPU |
| - | -: | -: | -: | -: |
| OpenVDN published, FP8, 8× B200 Ulysses 5+3 ([openvdn.github.io](https://openvdn.github.io/)) | 1.40 | 11.2 s | — | — |
| OpenVDN reference, BF16 (`8nfe_tuned.yaml`), 1× B200 | 7.66 | 61.2 s | — | — |
| OpenVDN reference, FP8, 1× B200 | 6.25 | 50.0 s | — | — |
| OpenVDN reference, FP8, 2× B200 Ulysses (standard / 1+1 branch-parallel) | 4.87 / 5.04 | 39.0 / 40.3 s | — | — |
| OpenVDN reference, FP8, 4× B200 Ulysses (3+1 branch-parallel) | 2.59 | 20.7 s | — | — |
| OpenVDN reference, FP8, 8× B200 Ulysses (5+3 / 4+4 / standard) | 1.40 / 1.47 / 1.52 | 11.2 / 11.7 / 12.2 s | — | — |
| SGLang, `--quantization bf16`, 1× B200 | 7.45 | 58.8 s | 8.8 s | 156,298 MB |
| SGLang, `--quantization fp8` (→ online `mxfp8` on SM100+), 1× B200 | 6.03 | 47.6 s | 8.7 s | 128,418 MB |
| SGLang default (online `mxfp8`), 2× B200 Ulysses2, `--performance-mode speed`, served-shape warmup | **3.30** | **25.9 s** | 4.7 s | 106,738 MB |
| SGLang, `--quantization bf16`, 4× B200 Ulysses4, `--performance-mode speed`, served-shape warmup | 2.06 | 16.2 s | 2.3 s | 108,434 MB |
| SGLang default (online `mxfp8`), 4× B200 Ulysses4, `--performance-mode speed`, served-shape warmup | **1.67** | **13.1 s** | 2.4 s | 85,244 MB |
| SGLang default (online `mxfp8`), 8× B200 Ulysses8, `--performance-mode speed`, served-shape warmup | **0.88** | **6.9 s** | 1.5 s | 79,972 MB |
| SGLang, per-channel fp8 weight scales (the online `fp8` path of other models; on SM100+ `--quantization fp8` resolves to `mxfp8`, so this arm selects the per-channel path explicitly), 8× B200 Ulysses8 | 0.98 | 7.7 s | 1.5 s | 79,504 MB |

On 8× B200 the SGLang Ulysses8 path runs the paper workload at 0.88 s/NFE
(7.0 GPU-seconds per NFE) against the published 1.40 s/NFE of OpenVDN's 5+3
branch-parallel layout; the whole request (text encoding, 8 forwards, joint
decode) completes in about 8.7 s after warmup. At this GPU count the step is
launch- and copy-bound in the linear branch rather than FLOP-bound, and the
transformer runs online `mxfp8` by default (e4m3 with one E8M0 scale per 32
elements into cuBLASLt's block-scaled GEMM, with the activation quant fused
into the adaLN and SwiGLU producers).

Against the published 8× B200 headline (1.40 s/NFE, 11.2
GPU-seconds per NFE), the SGLang 4× B200 run spends 6.7 GPU-seconds per
NFE, with half the GPUs and half the all-to-all fan-out. At equal GPU count
SGLang is 1.48× faster at two cards (3.30 vs 4.87 s/NFE), 1.55× at four
(1.67 vs 2.59) and 1.60× at eight (0.88 vs 1.40), while on one card the two
stacks are within 4% of each other (6.03 vs 6.25 s/NFE quantized, 7.45 vs 7.66
in BF16). The advantage is therefore parallel scaling rather than per-GPU
kernel work: SGLang holds 86-91% parallel efficiency from 2 to 8 cards against
the reference stack's 56-64%, and sweeping the reference stack's own layout
does not close it (its best split is 8% better than standard Ulysses at eight
cards, 1.40 against 1.52 s/NFE).

On one B200 both single-GPU rows run with the DiT resident under the auto
memory policy (128 GB peak quantized, 156 GB in BF16, of the card's 183 GB),
so no `--layerwise-offload-components` flag is needed at this clip length and
the single-GPU rows are compute-bound.

### VDN-H3 on RTX PRO 6000

The same paper workload (1344×768, 345 frames, 8 DiT forwards, seed 1000,
`hybrid_window_attn_h3`, `--performance-mode speed`, served-shape warmup) on
RTX PRO 6000 Blackwell Server Edition cards (SM120, 96 GB, PCIe, no NVLink).
The window runs on FA4's sm120 kernel and the transformer defaults to online
`mxfp8` (a cutlass sm120 block-scaled GEMM); `--quantization fp8` maps to it.
One card needs `--layerwise-offload-components text_encoder`: the fp8 DiT
(33 GB) and the bf16 text encoder (48 GB) do not both fit, and the whole-module
`--text-encoder-cpu-offload` re-homes the encoder in one piece and runs out of
memory when it is used. Multi-card Ulysses goes over PCIe; the all-to-all is
the bulk of the step there (54% of GPU time at 8 cards), so the per-card
efficiency drops with count. A 2-card run should use a pair on one PCIe
switch (`nvidia-smi topo -m` shows `PIX`): a pair across the CPU root complex
measured 15.7 to 17.0 s/NFE on a shared host.

| Config | Steady s/NFE | Denoise (8 forwards) | Decode | Peak/GPU |
| - | -: | -: | -: | -: |
| 1× RTX PRO 6000, `--layerwise-offload-components text_encoder` (default `mxfp8`) | **20.27** | **160.0 s** | 20.6 s | 95,790 MB |
| 1× RTX PRO 6000, same, per-channel `fp8` path (before this mapping) | 22.85 | 180.3 s | 20.7 s | 95,160 MB |
| 2× RTX PRO 6000 Ulysses2, one PCIe switch (default `mxfp8`) | 13.61 | 107.4 s | 10.6 s | 93,846 MB |
| 2× RTX PRO 6000 Ulysses2, one PCIe switch, per-channel `fp8` | 14.97 | 118.1 s | 10.6 s | 94,700 MB |
| 4× RTX PRO 6000 Ulysses4 (default `mxfp8`) | 7.98 | 63.0 s | 5.6 s | 82,178 MB |
| 4× RTX PRO 6000 Ulysses4, per-channel `fp8` | 8.23 | 65.1 s | 5.6 s | 82,422 MB |
| 8× RTX PRO 6000 Ulysses8 (default `mxfp8`) | 5.43 | 43.1 s | 3.5 s | 77,704 MB |
| 8× RTX PRO 6000 Ulysses8, per-channel `fp8` | 5.47 | 43.4 s | 3.5 s | 77,236 MB |

For comparison the same code on B200 measures 6.03 / 3.30 / 1.67 / 0.88 s/NFE
at 1 / 2 / 4 / 8 cards (NVLink, 86% parallel efficiency at 8).

### H200 topology comparison

The same four-card H200 host completed both lossless resident placements with
the standard 1344×768, 5-second, 50-step T2VA request (fixed prompt and seed,
eager BF16/FP32, back-to-back runs on an otherwise idle host). Latency is the
warmed-up request; the first pair uses the default warmup request, the second
pair adds `--warmup-resolutions 1344x768` so warmup already covers the served
resolution:

| Topology | Warmup | Denoise | Decode | E2E | Peak/GPU |
| - | - | -: | -: | -: | -: |
| Ulysses4 | default | 79.04 s | 3.77 s | **84.14 s** | 94,288 MB |
| TP2 + Ulysses2 | default | 81.17 s | 2.97 s | 85.51 s | 63,490 MB |
| Ulysses4 | `--warmup-resolutions 1344x768` | 71.73 s | 1.32 s | **74.38 s** | 94,290 MB |
| TP2 + Ulysses2 | `--warmup-resolutions 1344x768` | 75.52 s | 1.29 s | 78.33 s | 63,490 MB |

Ulysses4 stays the H200 latency default: 5.0 % faster end-to-end than
TP2 + Ulysses2 once warmup covers the served resolution (1.6 % with the
default warmup, where first-request cold start masks the topology gap).
TP2 + Ulysses2 shards the DiT weights and holds peak memory about 30 GB per
GPU lower, which is why it remains the 80 GB H100 recipe. Matching the warmup
request to the served resolution removes the cold first-request cost on both
topologies (about 10 s end-to-end on this workload).

### H200 cross-node scaling

Long references and long durations grow the packed sequence length, and
Ulysses alone cannot scale sequence parallelism past the GPU count of one
node without either violating head-count divisibility or exposing
all-to-all traffic across the slower inter-node link. H3 combines
node-local Ulysses with cross-node Ring: Ring's point-to-point KV rotation
is designed to overlap with attention compute, which fits a slower
cross-node link better than an all-to-all does.

**Hardware.** 2 nodes × 8× NVIDIA H200 SXM, same cluster, InfiniBand
between nodes.

**Serve command.** The cross-node cell the picker emits for H200, run
identically on both nodes with `--node-rank` set to 0 and 1:

```bash Command theme={null}
sglang serve \
  --model-path MiniMaxAI/MiniMax-H3 \
  --model-variant ref2va \
  --num-gpus 16 \
  --nnodes 2 \
  --node-rank {{NODE_RANK}} \
  --dist-init-addr {{NODE0_IP}}:20000 \
  --sp-degree 16 \
  --ulysses-degree 8 \
  --ring-degree 2 \
  --encoder-parallel replicate \
  --performance-mode speed \
  --host 0.0.0.0 \
  --port 30010
```

**What was measured.** A controlled denoise-stage comparison on identical
hardware: 8× H200 single-node (Ulysses8, no Ring) versus the same 16-GPU
cross-node command above (Ulysses8 × Ring2), holding prompt, seed, and
step count fixed:

| Task | Single-node (Ulysses8) | Cross-node (Ulysses8 × Ring2) | Change |
| - | -: | -: | -: |
| T2VA denoise/step | 0.749 s | 0.477 s | −36.3% |
| Ref2VA/V2V denoise/step | 2.572 s | 1.494 s | −41.9% |

The gain grows with sequence length because Ring's per-hop communication
cost stays roughly constant while attention compute grows quadratically
with sequence length, so V2V's longer packed sequence benefits more than
T2VA's shorter one. With the point-to-point KV rotation pipelined against
attention compute, one V2V request's full denoise stage completed in
68.1–68.3 seconds versus 128.6 seconds on the single-node 8-GPU baseline
(−47.0%), with byte-identical output to the unpipelined cross-node path.

Cross-node determinism was confirmed separately: the same request run
twice against the same cross-node deployment produced byte-identical
output. A cross-node run's output is not expected to bit-match a
single-node run of the same prompt and seed — Ring's online-softmax merge
across hops accumulates floating-point operations in a different order
than single-node attention, which is an expected source of bit-level
difference, not a correctness regression.

<Warning>
  `--encoder-parallel auto`'s fold decision is not yet node-boundary aware
  and attempts to fold the text encoder across nodes, which crashes the
  Ref2VA reference-conditioned encoder. Always pass
  `--encoder-parallel replicate` explicitly for cross-node H3 deployments.
</Warning>

### H100 topology comparison

The same four-card H100 host completed three lossless placements. TP2 with
Ulysses2 was the fastest; TP4 used the least memory:

| Topology | Pipeline latency | Peak/GPU |
| - | -: | -: |
| TP2 + Ulysses2 | 13.25 s | 66.04 GB |
| FSDP + Ulysses4 | 13.36 s | 57.01 GB |
| TP4 + Ulysses1 | 13.86 s | 49.80 GB |

### RTX 5090 capacity run (two cards)

The single-card recipe is a different one — see *RTX 5090 single card, physical
desktop* below; the twenty resident layers here are half a layer per GPU under
TP2 and do not transfer to one card. The verified two-card RTX 5090 host used TP2 with layerwise offload. The full
50-step, 1344×768, 5-second request completed in 559.67 seconds: 525.05
seconds of denoising and 33.61 seconds of decoding, with a 26.3 GiB sampled
peak per GPU.

| DiT settings | 5-step denoise | Inference | Peak/GPU | Result |
| - | -: | -: | -: | - |
| prefetch 1, resident 20 | 43.48 s | 78.11 s | 26.3 GiB | Selected recipe |
| prefetch 2, resident 20 | 43.37 s | 78.06 s | 27.5 GiB | No measurable gain |
| Ulysses2, prefetch 2, resident 10 | Did not reach warmup | — | — | Rejected |

### Consumer GPU tuning

On consumer hardware the binding question is not which card you have but how much
host RAM sits behind it. H3's weights are about 108 GB — 61.73 GB of DiT and
46.18 GB of text encoder — so no consumer configuration holds them all, and where
the shortfall lands decides the throughput.

**The command** — most consumer machines need exactly one flag beyond the model:

```bash consumer single GPU, lossless theme={null}
sglang serve --model-path MiniMaxAI/MiniMax-H3 --model-variant fl2va \
  --layerwise-offload-components dit,text_encoder,vae
```

With 16 GB of VRAM or more, add `--layerwise-resident-layers video_vae=36` for
the 13 s decode; with \~96 GB of host RAM and 16 GB+ of VRAM, add
`--dit-layerwise-resident-layers 4` for the 6 s step. That is the whole flag
surface. The [builder at the top of this page](#1-quick-start) has consumer
cards and a Host RAM selector: pick your budget and it emits this command with
your tier's measured expectations attached as comments. The table below is the
same data in one view.

**Unified-memory machines are the exception**: on a DGX Spark (GB10, 128 GB
shared between CPU and GPU) launch with **no flags at all**. The deployment
still exceeds the pool, automatic offload engages on its own, and its placement
beat the explicit recipe above by 2.1× on the denoise (12.1 vs 25.8 s/it
measured on the same box) — the VRAM/host split that justifies every flag in
this section does not exist there. Expect \~12 min of load, a \~5.5 min text
encoding stage per request (steady-state compute on this chip, not a stall),
and \~12 min per warm 480P request.

**Two budgets, and what each one buys**

| | 12 GB VRAM + 32 GB host | host free, VRAM 16 GB |
| - | -: | -: |
| Recipe | A | B |
| Peak VRAM | ≤ 12 GiB | ≤ 16 GiB (OOMs at 12) |
| Host anonymous (must fit) | 24.5 GiB | 116.7 GB pinned |
| Denoise, 864×480 / 124 frames / 20 NFE | 16.8 - 18.7 s/it | **6.01 s/it** |
| Runs at all | yes | yes |

The left column is one configuration measured twice, at 318.94 s and 356.37 s;
the 12% spread tracked host load on a shared machine, so treat smaller
differences than that as unresolved. The right column is 120.92 s at a 16 GiB
allocator cap. Four resident DiT layers is what Recipe B buys its speed with,
and it is also why 12 GiB is not enough for it.

Read the host row carefully, because the two numbers are not the same kind of
memory. *Anonymous* host memory — pinned buffers and pageable copies — has to fit,
and the kernel cannot reclaim it. Page cache backing a file mapping is
*droppable*, so it does not count against the budget even though it shows up in
`VmRSS`; use `RssAnon` from `/proc/<pid>/status` when checking. Likewise measure
VRAM with `torch.cuda.set_per_process_memory_fraction` and let the allocator fail,
rather than reading `nvidia-smi`, which reports the caching allocator's reserved
pool and overstates the requirement.

Inside 32 GB the weights cannot be pinned, so each denoise step copies about
60 GiB from the checkpoint mapping, and a mapped source is synchronous however
the copy is requested: the driver stages it through its own buffer, so the
transfer neither overlaps compute nor runs at pinned bandwidth. That is where
the step goes, and giving the host room to pin the weights instead is what takes
it to 6.01 s.

Two caveats on the constrained number, both from instrumenting the run rather
than from arithmetic. The machine it was measured on has 2 TB of host memory, so
the kernel kept all 107.7 GiB of mapped checkpoint pages resident: major faults
across a whole request were 6, and `read_bytes` was zero. Nothing was read from
disk. A real 32 GB host cannot cache 107.7 GiB, so it will fault and re-read,
and should be expected to be slower than the figures here rather than equal to
them — an NVMe is a requirement, not a recommendation. Measure your own machine
with major faults (`/proc/<pid>/stat`) on the worker process, not on the
launcher, which holds no weights.

**Recipe A — fits 12 GB VRAM + 32 GB host**

```bash 12 GB + 32 GB, lossless theme={null}
PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
sglang serve --model-path MiniMaxAI/MiniMax-H3 --model-variant fl2va \
  --performance-mode memory \
  --layerwise-offload-components dit,text_encoder,vae \
  --layerwise-resident-layers video_vae=36
```

**Recipe B — host memory is free (the fast path)**

```bash unconstrained host, lossless theme={null}
sglang serve --model-path MiniMaxAI/MiniMax-H3 --model-variant fl2va \
  --performance-mode memory \
  --layerwise-offload-components dit,text_encoder,vae \
  --dit-layerwise-resident-layers 4 \
  --layerwise-resident-layers video_vae=36
```

Recipe B pins \~112 GB of host memory (DiT 61.56 GB, text encoder 46.18 GB, VAE
\~4.5 GB in its decode dtype). Do not reach for it on a 32 GB machine.

**What not to change, and why**

* `video_vae=36` holds every decoder block for the decode only — residency
  arms at the decoder's first block and releases when it finishes, so the
  denoise still runs on an empty card. It fits 12 GB because decoder weights
  are held in their decode compute dtype (fp16) from load, which halves them
  to \~4.9 GiB; the rounding was already part of every output (the decode
  computes in fp16 autocast), so the result is bit-identical, and the decode
  drops from 60 s streamed (or 209 s on a busy host) to \~10 s. The
  `expandable_segments` line stays: the decode sits close enough to the cap
  that fragmentation otherwise tips it over.
* Leave `--enable-torch-compile` off, as elsewhere on this page. Layerwise offload
  rebinds `param.data` on every layer, so compiled graphs do not get the benefit
  they would on resident weights.
* Recipe A's flags are what the automatic policy should choose on its own. Until
  the model declares its own placement, `--performance-mode memory` plus the
  explicit component list is what makes it happen; pass them.

**Reading the startup log**

The server prints the memory decisions it made; checking three lines against
your budget catches a mis-set machine in the first minute instead of the first
request.

* `Layerwise offload: host memory available: N GiB` — what the runtime sees
  after loading, not your DIMM size. On a 32 GB host expect single digits here;
  a much larger number means another process's memory accounting (or a
  container limit) is in play.
* `leaving N GiB of weights on the checkpoint mapping` — the expected line on a
  32 GB host: the DiT streams from the checkpoint file. If instead the log
  reports pinned weights, the runtime decided your host has room — which is
  faster, and means the 32 GB figures above do not apply to you.
* `Loaded video_vae: ... host mmap` vs `host pageable` — where the VAE landed
  (decoder weights are \~4.9 GiB once held in their decode dtype).
  `Loaded <component>` lines carry the same buckets for every component.

If a request dies after the denoise finishes, it is the decode colliding with
the cap: keep the `expandable_segments` line, and if it persists drop to
`video_vae=24` and take the partially streamed decode.

**Against ComfyUI, on the same weights**

Same unpruned bf16 checkpoints, same card, same sampler settings (cfg 1.0,
euler\_ancestral, sigma shift 12.0/3.0, seed 1101), 864×480 / 124 frames / 20 NFE:

When host memory is free, the engines are close and sglang is ahead:

| | denoise | host anonymous | peak VRAM |
| - | -: | -: | -: |
| sglang, Recipe B | **6.01 s/it** | 116.7 GB pinned | ≤ 16 GiB |
| ComfyUI KSampler | 6.58–6.59 s/it | 116.5 GiB | 13048 MiB |

Inside 12 GB, both engines run these weights, and one measurement convention
matters on each side. ComfyUI's memory manager reads system RAM and adapts, so
the rows below patch `psutil` to a pretend host size — the same convention the
sglang rows use. Its `--reserve-vram` is also soft: told to keep 12 GiB free it
still peaked at 13.5 GiB, a figure a real 12 GB card cannot give it, so both
engines here run under the same hard allocator cap
(`set_per_process_memory_fraction`), where its peak stays at 12.1–12.3 GiB.
Under that cap, Recipe A wins the whole request at every host size:

| 12 GB VRAM, both engines hard-capped | sglang Recipe A (TE + denoise + decode) | ComfyUI, bf16 (warm) |
| - | -: | -: |
| 32 GB host | 12.4 + 212.8 + 9.4 ≈ **235 s** | 276–302 s |
| 48 GB host | 15.8 + 192.1 + 10.0 ≈ **218 s** | 246–267 s |
| 64 GB host | 7.5 + 162.4 + 10.3 ≈ **180 s** | 194–195 s |

Same GPU, same load window, unpruned bf16 checkpoints, outputs verified. All
figures are anchored at 480P — activations grow with the pixel count, so at
768P drop the resident DiT layers to 0 first, then `video_vae` to 24 if the
decode still collides.

One convention behind that table is the one a home machine does not share: the
host sizes above are `psutil` patches on a 2 TB lab machine, so the kernel kept
the whole checkpoint in page cache and nothing was read from disk. A physical
host of that size re-reads the streamed weights every step, and what that costs
is computable — `bytes per step = (50 - resident DiT layers - layers the host
pin budget covered) x 1.23 GiB`. The courier overlaps that read with compute, so
the drive stops being the denoise clock once its sequential rate exceeds those
bytes divided by the card's step time. A 32 GB host covers roughly 13 layers,
which puts a 24 GB card at \~38 GiB/step and a 12/16 GB card at \~46 GiB/step: a
PCIe 4.0 NVMe (6–7 GiB/s) clears both, a PCIe 3.0 drive (\~3.2 GiB/s) becomes the
clock, and a SATA SSD is not usable for this model. Resident DiT layers remove
1.23 GiB/step each, but only while the streamed set is larger than the pin
budget — once the read fits under the step's compute time, more of them buy
nothing and only cost VRAM.

An earlier revision of this page recorded 38 s/step on a physical 4090 with a
990 Pro, reading 52.9 GB per step — about 1.4 GB/s, and faulted sequentially, so
almost none of it showed in `majflt` (measure `read_bytes`, not major faults).
That rate was the old path, not the drive: the pinned-store pool rounded every
block up to a power of two, so the pin budget over-committed and the machine had
to run with pinning off, and the layers that stayed mapped faulted in 4 KiB at a
time. Exact-size pinning and O\_DIRECT reads through the courier removed both,
and the physical 5090 run below shows the drive fully hidden behind compute. A
4090 on a 32 GB host is derived to return to its compute wall (\~8.5 s/step) on a
PCIe 4.0 drive; that machine has not been re-measured. The
VRAM axis holds too: capped at 16 GiB the same recipe wins \~250 vs 292–301 s,
and at 24 GiB (with `--dit-layerwise-resident-layers 6` — measured at a
22 GiB cap so a desktop's own allocations fit; a headless card can raise it
to 10 for under 1% more) \~8.5 s/step vs ComfyUI's 249–260 s requests. Four changes carry it: the VAE staying on its checkpoint mapping (#35862, root fix
\#35946), per-layer pinning with net-cost accounting (#35867), the courier
thread that ships still-mapped layers through pinned slots (#35882), and
decoder weights held in their decode dtype from load (#35967) — which is what
lets `video_vae=36` fit and turns the decode from the slowest stage (54–96 s
streamed) into the fastest (\~10 s, faster than ComfyUI's own 15–25 s). Output
equivalence is bit-level: the fp16-held decode reproduced the fp32-store run's
video byte for byte, and the audio stream is bit-identical.

Stage by stage under the cap: text encoding is even (both stream the same
48 GB Qwen3VL), the denoise leads at 32–48 GB hosts and sits within
run-to-run variance of ComfyUI at 64 GB (162 vs 159 s), and the decode leads
everywhere. Two ComfyUI notes that still matter: `--fast-disk` measured no
faster than its default on this lab host — though on a physical 60 GB desktop
its default is OOM-killed while loading (55.6 GB of anonymous memory) and
`--fast-disk` is what makes it run at all — and stacking
`--novram --cache-none --disable-pinned-memory` made things strictly worse
(69.1 GiB anonymous, 750 s requests) — the adaptive default is the right
configuration on a small host.

The path ComfyUI ships for 12 GB cards uses
`minimax_h3_fl2va_pruned_int8_convrot` and `qwen3vl_32b_minimax_h3_nvfp4_awq`,
i.e. an int8 DiT and an NVFP4 text encoder, and its pruned bf16 file is 40.2 GB
against the unpruned 66.3 GB. Those are different weights, so it is not a
like-for-like comparison with the recipes above.

### RTX 5090 single card, physical desktop

Every consumer figure above this point was measured with at least one axis
simulated: a hard allocator cap standing in for VRAM, a `psutil` patch standing
in for host RAM, and a lab machine whose page cache held the whole checkpoint.
This run had none of that — an RTX 5090 (32 GB) in a Ryzen 9 9950X desktop with
60 GB of RAM, an 8 GB swap file and a Samsung 9100 PRO on PCIe 5.0, on Ubuntu
24.04. The workload matches the comparison above: FL2VA T2VA, 864×480, 124
frames, 20 steps, cfg 1.0, euler\_ancestral, sigma shift 12.0/3.0, same seeds and
keyframes on both engines, unpruned bf16 weights on both sides.

```bash 32 GB VRAM + 60 GB host, lossless theme={null}
PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
sglang serve --model-path MiniMaxAI/MiniMax-H3 --model-variant fl2va \
  --performance-mode memory \
  --layerwise-offload-components dit,text_encoder,vae \
  --dit-layerwise-resident-layers 14 \
  --layerwise-resident-layers video_vae=36 \
  --warmup-resolutions 864x480 --warmup-num-frames 124
```

| | text encoding | denoise step | decode | request |
| - | -: | -: | -: | -: |
| sglang | **4.2 s** | 5.14–5.17 s | **6.4 s** | **112.2 / 112.1 s** |
| ComfyUI `--fast-disk --cache-none` | 11.3 s | 5.17–5.30 s | 7.2 s | 145.9 / 145.6 / 140.9 s |

Three things a capped run cannot settle:

**The denoise is at the card's compute wall, not the drive's.** The two engines
are within noise of each other per step, and prefetch depth 2 and cuDNN SDPA
each measured as no change. The 51.3 GiB the DiT reads from the drive per
request is fully overlapped — across a whole request the compute thread waited
0.9 s for the courier.

**Host RAM decides how much traffic there is to hide.** At 60 GB the pin budget
covers 34 of the 50 DiT layers, so only about two layers per step come off the
disk. The 46 GiB text encoder is never pinned — streamed layers take the budget
first — and is read whole, once per request, at the drive's sequential rate:
that is the 4.2 s stage, against 12.1–12.9 s when the same bytes go through the
page cache.

**Neither engine's default command survives this machine.** ComfyUI's is
OOM-killed during load (55.6 GB of anonymous memory) and needs `--fast-disk`;
sglang's was too, until pinned stores were allocated at their exact size instead
of the allocator's next power of two. Even now the automatic placement keeps no
resident DiT layers and streams the VAE on every decode, so the flags above are
what reach the numbers in the table.

Two caveats to carry into your own machine. The run finished with its 8 GB swap
file full, because the pin budget takes 95% of the memory available after
loading and there is no smaller setting yet — on a machine that is also your
desktop, expect that pressure and give the card more resident layers, since a
resident layer is one the host no longer has to pin. And these are 480P figures:
at 768P drop the resident DiT layers first, then `video_vae` to 24.

### RTX 4090 24 GB single-GPU run

One RTX 4090 D 24 GB completed the 1344×768, 107-frame, 20-NFE T2VA
workload (euler, `torch.compile` and step caching disabled) with DiT and
text-encoder layerwise offload. Same process: load → warmup (seed 0) →
timed (seed 42); only the timed pass is reported. GPU peak stayed about
18 GB.

| Config | Timed e2e | Denoise | vs BF16 | PSNR vs BF16 |
| - | -: | -: | -: | -: |
| BF16 + FlashAttention | 405.6 s | 370.2 s | 1.00× | — |
| `kitchen_int8` + FA | 303.3 s | 273.7 s | 1.34× | 24.81 dB |
| `kitchen_int8` + `sol_attn` | 223.9 s | 203.9 s | 1.81× | 24.44 dB |
| `kitchen_int8` + `sage_attn` | 174.9 s | 154.2 s | 2.32× | 23.51 dB |
| `kitchen_int8` + Sage→Sol hybrid | 163.8 s | 143.1 s | 2.48× | 23.04 dB |

`kitchen_int8` + FA changes Linear numerics only. The `sol_attn` /
`sage_attn` / hybrid rows also change the attention algorithm, so speed
and pixel fidelity rank in opposite orders there. Default remains
`kitchen_int8` + `fa`. Cache-DiT can share this layerwise recipe; omit
`quality` and see the quality-level section.

Same card, 50-step run. Speedup is vs BF16+FA. PSNR is vs same-tree INT8+FA (Spectrum off).

| Config | e2e | vs BF16+FA | PSNR mean / min |
| - | -: | -: | -: |
| BF16 + FlashAttention | 1008.6 s | 1.00× | — |
| `kitchen_int8` + FA | 718.5 s | 1.40× | — |
| `kitchen_int8` + FA + Spectrum `11 / 5 / 1.0` | **285.3 s** | **3.53×** | **27.4 / 25.3** |
| `kitchen_int8` + `sage_attn` + Spectrum `15 / 5 / 0` | 245.3 s | 4.11× | 23.6 / 21.0 |
| `kitchen_int8` + Sage→Sol hybrid + Spectrum `15 / 5 / 0.5` | **219.0 s** | **4.61×** | 20.6 / 17.8 |

Prefer FA + `11 / 5 / 1.0` when the bar is FA-like. Sage / hybrid + Spectrum is faster; quality then follows the attention backend, not the skip schedule.

### AMD Instinct task and scaling runs

The AMD recipes keep the released BF16/FP32 precision policy and use AITER
packed attention. The picker emits the fastest measured topology, 8 GPUs with
Ulysses degree 8. All runs below completed full H.264/AAC decoding and
representative-frame inspection.

| Hardware | Task | Denoise | Decode | Peak/GPU |
| - | - | -: | -: | -: |
| MI355X | T2VA | 55.2907 s | 9.5344 s | 97,444 MB |
| MI355X | FL2VA | 53.7978 s | 9.4477 s | 96,922 MB |
| MI355X | Ref2VA | 41.3812 s | 6.8247 s | 94,518 MB |
| MI300X | T2VA | 167.4878 s | 25.3244 s | 97,272 MB |
| MI300X | FL2VA | 150.2311 s | 12.5684 s | 96,750 MB |
| MI300X | Ref2VA | 107.6232 s | 11.3768 s | 94,268 MB |

The task matrix used 8 GPUs and 50 denoising steps. The scaling matrix uses
one 1344×768, 209-frame T2VA request and changes only the GPU count and
matching Ulysses degree:

| Hardware | GPUs | Denoise | Decode | Peak/GPU |
| - | -: | -: | -: | -: |
| MI355X | 8 | 55.2907 s | 9.5344 s | 97,444 MB |
| MI355X | 4 | 104.2294 s | 11.1824 s | 103,350 MB |
| MI355X | 2 | 223.0246 s | 15.5330 s | 115,250 MB |
| MI355X | 1 | 288.7968 s | 24.0472 s | 137,676 MB |
| MI300X | 8 | 167.4878 s | 25.3244 s | 97,272 MB |
| MI300X | 4 | 297.3727 s | 26.5067 s | 103,436 MB |
| MI300X | 2 | 585.5401 s | 29.4909 s | 115,010 MB |
| MI300X | 1 | 978.0886 s | 36.0142 s | 137,626 MB |

For a measured lower-count AMD deployment, set both `--num-gpus` and
`--ulysses-degree` to 4, 2, or 1. AITER packed attention matched segment-wise
BF16 SDPA at cosine similarity `0.9999991655` on MI355X and `0.9999991059` on
MI300X.

## 10. Run in ComfyUI

<ComfyUISupport model="minimax-h3" />


---

## Appendix A: MDX components and recipe data

Verbatim copy of the component definitions and the `config` object that the live page uses
to render the interactive deployment builder (hardware profiles, precision, parallelism,
DLO/offload options) and the ComfyUI/benchmark tables. Re-publishing this file as MDX
requires this block to appear above its first use.

```jsx
export const ComfyUISupport = ({model = "video", note}) => {
  const PLUGIN_PATH = "python/sglang/multimodal_gen/apps/ComfyUI_SGLDiffusion";
  const PLUGIN_URL = "https://github.com/sgl-project/sglang/tree/main/" + PLUGIN_PATH;
  const MODELS = {
    flux: {
      serverNode: "SGLDiffusion Generate Image",
      integratedKey: "flux",
      workflow: "flux_sgld_sp.json"
    },
    "z-image": {
      serverNode: "SGLDiffusion Generate Image",
      integratedKey: "lumina2",
      workflow: "z-image_sgld.json"
    },
    "qwen-image": {
      serverNode: "SGLDiffusion Generate Image",
      integratedKey: "qwen_image",
      workflow: "qwen_image_sgld.json"
    },
    "qwen-image-edit": {
      serverNode: "SGLDiffusion Generate Image",
      integratedKey: "qwen_image_edit",
      note: "Image editing through the integrated path is experimental."
    },
    "minimax-h3": {
      serverNode: "SGLDiffusion Generate H3",
      verified: true,
      integratedBlockedBecause: "H3 denoises a packed video-and-audio sequence in one pass and routes " + "conditioning by task, while ComfyUI's KSampler drives a single latent " + "tensor and has no audio branch"
    },
    image: {
      serverNode: "SGLDiffusion Generate Image"
    },
    video: {
      serverNode: "SGLDiffusion Generate Video"
    }
  };
  const spec = MODELS[model] || MODELS.video;
  const extraNote = note || spec.note;
  return <div>
      <p>
        Run this model from ComfyUI with the{" "}
        <a href={PLUGIN_URL}>SGLDiffusion plugin</a>, which ships in the SGLang
        repository at <code>{PLUGIN_PATH}</code>.
      </p>

      <p>
        <strong>Server mode</strong> — SGLang runs the pipeline and ComfyUI
        sends the request. Start a server as shown above, point the{" "}
        <code>SGLDiffusion Server Model</code> node at it, then generate with{" "}
        <code>{spec.serverNode}</code>.
        {spec.verified ? " This path has been run end to end against a live server." : ""}
      </p>

      {spec.integratedKey ? <p>
          <strong>Integrated mode</strong> — ComfyUI's own sampler, CLIP, and
          VAE drive the loop while SGLang replaces the model forward. Load the
          checkpoint with <code>SGLDiffusion UNET Loader</code> and set{" "}
          <code>model_type</code> to <code>{spec.integratedKey}</code> on the{" "}
          <code>SGLDiffusion Options</code> node.
          {spec.workflow ? <>
              {" "}
              A reference workflow is included at{" "}
              <code>{`${PLUGIN_PATH}/workflows/${spec.workflow}`}</code>.
            </> : null}
        </p> : <p>
          <strong>Integrated mode</strong> — not available for this model
          {spec.integratedBlockedBecause ? `: ${spec.integratedBlockedBecause}` : ", which has no executor in the plugin"}
          . Use server mode.
        </p>}

      {extraNote ? <p>{extraNote}</p> : null}
    </div>;
};

export const config = (() => {
  const CONSUMER_12G = ["rtx4070", "rtx5070", "rtx3060"];
  const CONSUMER_16G = ["rtx4080", "rtx5080", "rtx5070ti", "rtx4060ti"];
  const CONSUMER_24G = ["rtx4090", "rtx3090"];
  const CONSUMER_32G = ["rtx5090"];
  const WORKSTATION_48G = ["rtx6000ada"];
  const WORKSTATION_96G = ["rtxpro6000"];
  const UNIFIED_128G = ["dgx-spark"];
  const CONSUMER_SINGLE = [...CONSUMER_12G, ...CONSUMER_16G, ...CONSUMER_24G, ...CONSUMER_32G, ...WORKSTATION_48G, ...WORKSTATION_96G, ...UNIFIED_128G];
  const CONSUMER_VRAM_16_PLUS = [...CONSUMER_16G, ...CONSUMER_24G, ...CONSUMER_32G];
  const CONSUMER_AMPERE = ["rtx3060", "rtx3090"];
  function consumerSingleCard(s) {
    return CONSUMER_SINGLE.includes(s.hw) && !(CONSUMER_32G.includes(s.hw) && Number(s.gpus_per_node) > 1);
  }
  function consumerFlags(s) {
    if (UNIFIED_128G.includes(s.hw)) return unified128Flags();
    if (WORKSTATION_96G.includes(s.hw)) return workstation96Flags();
    const flags = ["--performance-mode memory", "--layerwise-offload-components dit,text_encoder,vae", "--layerwise-resident-layers video_vae=36"];
    if (CONSUMER_VRAM_16_PLUS.includes(s.hw) && s.host_ram === "ram96") {
      flags.push("--dit-layerwise-resident-layers 4");
    }
    if (CONSUMER_24G.includes(s.hw) && s.host_ram === "ram32") {
      flags.push("--dit-layerwise-resident-layers 6");
    }
    if (CONSUMER_32G.includes(s.hw) && s.host_ram !== "ram96") {
      flags.push("--dit-layerwise-resident-layers 14");
    }
    if (WORKSTATION_48G.includes(s.hw)) {
      flags.push("--dit-layerwise-resident-layers 40");
    }
    return flags;
  }
  function unified128Flags() {
    return [];
  }
  function workstation96Flags() {
    return ["--performance-mode memory", "--layerwise-offload-components text_encoder,vae", "--layerwise-resident-layers video_vae=36"];
  }
  function consumerHints(s) {
    const hints = [];
    const bigHost = s.host_ram === "ram96";
    const midHost = s.host_ram === "ram64";
    if (bigHost) {
      if (CONSUMER_VRAM_16_PLUS.includes(s.hw)) {
        hints.push(CONSUMER_32G.includes(s.hw) ? "with the DiT pinned the denoise runs at this card's compute wall, measured at 5.14-5.17 s per step on a physical 5090; the decode holds all 36 blocks in their fp16 decode dtype and takes ~6.4 s" : "verified end to end: ~6 s per denoise step, 13 s decode");
        hints.push("fewer resident layers than the 32 GB rows is not a typo: with the DiT pinned in a big host, streamed layers arrive at pinned-copy speed and GPU residency buys little; on a 32 GB host the stream is the bottleneck residency cuts");
      } else {
        hints.push("~6 s per step once the host pins the DiT; the decode holds all 36 blocks in their fp16 decode dtype and takes ~10 s");
      }
      return hints;
    }
    if (CONSUMER_32G.includes(s.hw)) {
      if (midHost) {
        hints.push("measured end to end on a physical desktop (RTX 5090, 60 GB host, PCIe 5.0 NVMe -- VRAM, host RAM and drive all real): 112.2 / 112.1 s per request at 864x480 / 124 frames / 20 steps, against ComfyUI's 140.9-145.9 s on the same weights and sampler settings. Text encoding 4.2 s, denoise 98.0-98.5 s (5.14-5.17 s/step), decode 6.4 s");
        hints.push("at this host size the pin budget covers 34 of the 50 DiT layers, so only ~2 layers per step and the 46 GiB text encoder come off the drive -- 96 GiB per request, read with O_DIRECT and fully hidden behind compute (the compute thread waited 0.9 s per request)");
      } else {
        hints.push("derived, not yet measured at this host size: the card holds the same fourteen resident layers, but the pin budget covers only ~13 of the 36 streamed layers, so ~28 GiB per step comes off the drive. That stays hidden behind the 5.14 s/step compute wall on a drive delivering ~5.5 GiB/s or better (a PCIe 4.0 NVMe); a PCIe 3.0 drive becomes the clock at ~8.8 s/step");
      }
    } else if (CONSUMER_24G.includes(s.hw)) {
      hints.push("measured at 32 GB host under a 22 GiB cap (desktop headroom): ~8.5 s per denoise step with six resident layers, ~9.6 s decode -- ahead of ComfyUI (249-260 s at the 24 GiB cap); a headless card can raise to ten layers for under 1% more");
    } else if (CONSUMER_16G.includes(s.hw)) {
      hints.push("measured at 32 GB host: ~11.9 s per denoise step, ~11 s decode, ~250 s per request -- ahead of ComfyUI (292-301 s) under the same hard 16 GiB cap");
    } else {
      hints.push("measured at 32 GB host: ~10.6 s per denoise step, ~9.4 s decode, ~235 s per request -- ahead of ComfyUI (276-302 s) on the same weights under the same hard 12 GiB cap, output bit-identical");
    }
    if (CONSUMER_AMPERE.includes(s.hw)) {
      hints.push("the recipe and its memory behavior are tier-exact for this card; the step times above were measured on 40-series compute, and Ampere lands above them");
    }
    if (UNIFIED_128G.includes(s.hw)) {
      return ["verified on DGX Spark at 480P: ~12.1 s per denoise step steady-state, ~40 s decode, ~12 min per warm request -- with no flags at all; adding the discrete-GPU offload flags measured 2.1x slower on the same box", "the text encoder runs ~5.5 min per request and does not warm up: it is steady-state compute on this chip, not a stall -- budget for it", "expect ~12 min of server load before the first request; the first request itself runs at full speed (no JIT tax was measured)", "step times sit above the discrete-GPU rows because the GB10's ~273 GB/s memory bandwidth is the denoise ceiling, not the placement"];
    }
    if (WORKSTATION_96G.includes(s.hw)) {
      hints.push("derived recipe, not yet verified: 96 GB holds the whole 61.7 GB DiT resident, so only the text encoder and VAEs stream -- expect near-datacenter step times rather than the offload figures above");
    }
    if (WORKSTATION_48G.includes(s.hw)) {
      hints.push("derived recipe, not yet verified: 48 GB holds forty of the fifty DiT layers; the figures above are the 24 GB tier's and this card should land well under them");
    }
    hints.push("run with PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True -- the decode sits close enough to the cap that fragmentation otherwise tips it over");
    if (midHost) {
      hints.push("measured on a 12 GB card at a 48 GB host: ~9.6 s/step, ~218 s per request (ComfyUI 246-267 s); at 64 GB: ~8.1 s/step, ~180 s (ComfyUI 194-195 s); larger cards land at or below these");
    } else {
      hints.push("a 32 GB host cannot cache the 108 GB checkpoint, so the drive is in the loop on every step -- see the rate it has to clear, below. A physical desktop that clears it reaches the quoted step times rather than falling short of them");
    }
    hints.push('the startup log should say "leaving ... GiB of weights on the checkpoint mapping" -- if it does not, the host is not the constraint you set');
    hints.push("every figure here is anchored at 480P: activations grow with the pixel count, so at 768P drop the resident DiT layers to 0 first, then video_vae to 24 if the decode still collides -- the flags trade speed for headroom in that order");
    hints.push("what the drive has to deliver is computable: each step re-reads (50 - resident DiT layers - the layers the host pin budget covered) x 1.23 GiB, and the courier overlaps that read with compute, so the drive stops being the denoise clock once its sequential rate exceeds those bytes divided by this card's step time. A 32 GB host covers ~13 layers, which puts a 24 GB card at ~38 GiB/step and a 12/16 GB card at ~46 GiB/step: a PCIe 4.0 NVMe (6-7 GiB/s) clears both, a PCIe 3.0 drive (~3.2 GiB/s) becomes the clock, and a SATA SSD is not usable for this model");
    hints.push("the 38 s/step a physical 4090 with a 990 Pro once measured (52.9 GB per step at ~1.4 GB/s) was the old path: the pinned-store pool rounded every block up to a power of two, so the pin budget over-committed and the machine had to run with pinning off, and the layers that stayed mapped faulted in 4 KiB at a time. Exact-size pins and O_DIRECT reads through the courier removed both -- on the physical 5090 the drive is now fully hidden behind compute. A 4090 on a 32 GB host is derived to return to its compute wall (~8.5 s/step) on a PCIe 4.0 drive; that machine has not been re-measured");
    hints.push("resident DiT layers still remove 1.23 GiB/step of drive traffic each, but only while the streamed set is larger than the pin budget; once the read fits under the step's compute time, more resident layers buy nothing and only cost VRAM");
    hints.push("warm at the shape you will serve (--warmup-resolutions WxH --warmup-num-frames N): the default warmup is 1344x768 x 124 frames, which cost 7.6 min of startup on the physical 5090 and sizes the residency plan for a request you are not going to make");
    hints.push("the pin budget takes 95% of the memory available after loading, so on a machine you also use as a desktop expect swap pressure: the 5090 run above finished with its 8 GB swap file full. There is no supported knob for a smaller budget yet; giving the card more resident layers is the lever that exists, since a resident layer is one the host no longer has to pin");
    hints.push("on Windows run under WSL2, and keep the checkpoint inside the ext4 side (under ~), never on /mnt/c -- the NTFS bridge reads an order of magnitude slower and multiplies the disk clock");
    return hints;
  }
  return {
    modelName: "MiniMax-H3",
    supportedHardware: ["b200", "b300", "gb300", "gb200", "h200", "h100", "mi300x", "mi355x", "rtxpro6000", "rtx6000ada", "dgx-spark", "rtx5090", "rtx4090", "rtx3090", "rtx5080", "rtx5070ti", "rtx4080", "rtx4060ti", "rtx5070", "rtx4070", "rtx3060"],
    hardware: [{
      id: "rtxpro6000",
      label: "RTX PRO 6000",
      vram: "96GB",
      vendor: "consumer"
    }, {
      id: "rtx6000ada",
      label: "RTX 6000 Ada",
      vram: "48GB",
      vendor: "consumer"
    }, {
      id: "rtx5090",
      label: "RTX 5090",
      vram: "32GB",
      vendor: "consumer"
    }, {
      id: "rtx4090",
      label: "RTX 4090",
      vram: "24GB",
      vendor: "consumer"
    }, {
      id: "rtx3090",
      label: "RTX 3090",
      vram: "24GB",
      vendor: "consumer"
    }, {
      id: "rtx5080",
      label: "RTX 5080",
      vram: "16GB",
      vendor: "consumer"
    }, {
      id: "rtx5070ti",
      label: "RTX 5070 Ti",
      vram: "16GB",
      vendor: "consumer"
    }, {
      id: "rtx4080",
      label: "RTX 4080",
      vram: "16GB",
      vendor: "consumer"
    }, {
      id: "rtx4060ti",
      label: "RTX 4060 Ti",
      vram: "16GB",
      vendor: "consumer"
    }, {
      id: "rtx5070",
      label: "RTX 5070",
      vram: "12GB",
      vendor: "consumer"
    }, {
      id: "rtx4070",
      label: "RTX 4070",
      vram: "12GB",
      vendor: "consumer"
    }, {
      id: "rtx3060",
      label: "RTX 3060",
      vram: "12GB",
      vendor: "consumer"
    }],
    groupHardware: false,
    matchDims: [],
    overlayDims: [{
      id: "host_ram",
      title: "Host RAM",
      scope: "serve",
      description: "System memory decides where the DiT weights wait between steps: pinned when they fit, on the checkpoint mapping when they do not.",
      default: "ram32",
      showWhen: s => CONSUMER_SINGLE.includes(s.hw) && !UNIFIED_128G.includes(s.hw),
      options: [{
        id: "ram32",
        label: "32 GB"
      }, {
        id: "ram64",
        label: "48-64 GB"
      }, {
        id: "ram96",
        label: "96 GB+"
      }]
    }, {
      id: "weights",
      title: "Checkpoint Weights",
      scope: "base",
      description: "Choose the checkpoint partition required by the request mode.",
      default: "fl2va",
      options: [{
        id: "fl2va",
        label: "FL2VA",
        subtitle: "(First-and-Last-Frame-to-Video-and-Audio)",
        flags: ["--model-variant fl2va"]
      }, {
        id: "ref2va",
        label: "Ref2VA",
        subtitle: "(Reference-to-Video-and-Audio)",
        flags: ["--model-variant ref2va"]
      }]
    }, {
      id: "mode",
      title: "Request Mode",
      scope: "base",
      description: "The visible modes follow the selected checkpoint.",
      default: "t2va",
      options: [{
        id: "t2va",
        label: "Text only",
        showWhen: s => s.weights === "fl2va"
      }, {
        id: "i2va",
        label: "First frame",
        showWhen: s => s.weights === "fl2va"
      }, {
        id: "l2va",
        label: "Last frame",
        showWhen: s => s.weights === "fl2va"
      }, {
        id: "fl2va",
        label: "First + last frames",
        showWhen: s => s.weights === "fl2va"
      }, {
        id: "ref_image",
        label: "Image reference",
        showWhen: s => s.weights === "ref2va"
      }, {
        id: "ref_image_audio",
        label: "Image + audio",
        showWhen: s => s.weights === "ref2va"
      }, {
        id: "v2v",
        label: "Video reference",
        showWhen: s => s.weights === "ref2va"
      }, {
        id: "video_audio",
        label: "Video + soundtrack",
        showWhen: s => s.weights === "ref2va"
      }, {
        id: "audio_only",
        label: "Audio reference",
        showWhen: s => s.weights === "ref2va"
      }, {
        id: "mixed_ref",
        label: "Mixed references",
        showWhen: s => s.weights === "ref2va"
      }]
    }, {
      id: "placement",
      title: "Placement",
      scope: "serve",
      docsHref: "/docs/sglang-diffusion/api/cli#component-residency",
      description: "Keep weights resident for latency; shard or offload only when capacity requires it.",
      quality: "Memory policy",
      learnMore: "#7-feature-contracts-and-advanced-recipes",
      default: "resident",
      options: [{
        id: "auto",
        label: "Auto",
        flags: s => {
          if (consumerSingleCard(s)) return consumerFlags(s);
          const recipe = config.commandBuilder.resource.verifiedRecipes.find(entry => entry.hw === s.hw && entry.nodes === Number(s.nodes) && entry.gpus_per_node === Number(s.gpus_per_node));
          const placement = recipe?.placement || (s.hw === "rtx5090" ? "offload" : "resident");
          if (placement === "fsdp") return ["--performance-mode speed", "--use-fsdp-inference true"];
          return placement === "offload" ? ["--performance-mode memory", "--layerwise-offload-components dit,text_encoder,vae", "--dit-layerwise-resident-layers 20"] : ["--performance-mode speed"];
        },
        hints: s => consumerSingleCard(s) ? consumerHints(s) : [],
        description: "Use the recommended placement for the selected hardware and resource shape."
      }, {
        id: "resident",
        label: "Resident",
        flags: ["--performance-mode speed"],
        disabled: s => CONSUMER_SINGLE.includes(s.hw),
        disableReason: "The 61.7 GB DiT cannot be resident on a single consumer card.",
        recommendedWhen: s => s.hw !== "rtx5090" && !CONSUMER_SINGLE.includes(s.hw),
        description: "Lowest-latency path when the full pipeline fits in aggregate GPU memory."
      }, {
        id: "fsdp",
        label: "FSDP",
        flags: ["--performance-mode speed", "--use-fsdp-inference true"],
        soft: s => !["b200", "b300", "h200", "h100"].includes(s.hw) || s.nodes > 1,
        softReason: "Verified on single-node B200/B300/H200/H100. Other hardware and multi-node runs take the same flags but have not been through a verification round.",
        description: "Reduces resident DiT memory but adds parameter collectives on every block."
      }, {
        id: "offload",
        label: "Layerwise offload",
        flags: s => {
          if (consumerSingleCard(s)) return consumerFlags(s);
          return ["--performance-mode memory", "--layerwise-offload-components dit,text_encoder,vae", "--dit-layerwise-resident-layers 20"];
        },
        hints: s => consumerSingleCard(s) ? consumerHints(s) : [],
        soft: s => s.hw !== "rtx5090" && !CONSUMER_SINGLE.includes(s.hw),
        softReason: "Tuned and verified on the consumer cards. It runs on the datacenter GPUs too, where a resident recipe is simply faster.",
        recommendedWhen: s => s.hw === "rtx5090" || CONSUMER_SINGLE.includes(s.hw),
        description: "Capacity-first PCIe path. It is substantially slower than a resident datacenter recipe."
      }]
    }, {
      id: "attention",
      title: "Attention",
      scope: "serve",
      docsHref: "/docs/sglang-diffusion/attention_backends",
      description: "Select the packed-attention kernel used by H3 transformer modules.",
      quality: "Kernel policy",
      learnMore: "#7-feature-contracts-and-advanced-recipes",
      default: "platform",
      options: [{
        id: "platform",
        label: "Automatic",
        flags: s => ["mi300x", "mi355x"].includes(s.hw) ? ["--attention-backend aiter"] : [],
        env: s => ["mi300x", "mi355x"].includes(s.hw) ? ["SGLANG_USE_AITER=1"] : [],
        recommended: true,
        description: "Applies the verified backend policy for the selected hardware."
      }, {
        id: "fa",
        label: "FlashAttention",
        flags: ["--attention-backend fa"],
        disabled: s => ["mi300x", "mi355x"].includes(s.hw),
        disableReason: "Use the verified AITER platform default on AMD.",
        description: "An explicit native-dtype CUDA comparison path; reduction ordering may still differ."
      }, {
        id: "sage",
        label: "SageAttention",
        flags: ["--attention-backend sage_attn"],
        disabled: s => ["mi300x", "mi355x"].includes(s.hw),
        disableReason: "SageAttention is not exposed for the AMD recipes.",
        description: "Approximate attention math. Install its packed-varlen dependency and inspect video and audio quality."
      }]
    }, {
      id: "precision",
      title: "Precision",
      scope: "serve",
      docsHref: "/docs/sglang-diffusion/quantization",
      description: "Choose native mixed precision or a validated online weight quantization path.",
      quality: "Weight precision",
      learnMore: "#7-feature-contracts-and-advanced-recipes",
      default: "native",
      options: [{
        id: "native",
        label: "BF16 / FP32",
        recommended: true,
        description: "Reference mixed precision: BF16 transformer weights with required projections retained in FP32."
      }, {
        id: "fp8",
        label: "Online FP8",
        flags: ["--quantization fp8"],
        soft: s => !["b200", "b300"].includes(s.hw) || !["auto", "resident"].includes(s.placement) || s.nodes !== 1,
        softReason: "Verified for resident single-node B200/B300. Other hardware and placements take the same flag, but those recipes have not been verified yet.",
        description: "Approximate transformer weight quantization with the required H3 projections protected."
      }]
    }, {
      id: "encoder",
      title: "Encoder",
      scope: "serve",
      docsHref: "/docs/sglang-diffusion/encoder_parallel",
      description: "Control text-encoder work placement independently from DiT topology.",
      quality: "Parallel policy",
      learnMore: "#7-feature-contracts-and-advanced-recipes",
      default: "auto",
      options: [{
        id: "auto",
        label: "Auto",
        flags: s => s.nodes > 1 ? ["--encoder-parallel replicate"] : [],
        recommended: true,
        description: "Folds on verified single-host P2P systems and resolves to replicate across nodes."
      }, {
        id: "dp",
        label: "Data parallel",
        flags: ["--encoder-parallel dp"],
        disabled: s => CONSUMER_SINGLE.includes(s.hw) || (s.topology_mode === "manual" ? Number(s.tp_size) : config.commandBuilder.resource.autoTopology(s).tp_size) > 1,
        disableReason: "Encoder DP requires TP1 and a multi-GPU DP group; TP > 1 and the single-card consumer recipes do not qualify.",
        soft: s => s.nodes > 1,
        softReason: "Runs across nodes, but the measured 1.9× encode speedup comes from a single-node 2× H100 run; cross-node encoder DP is unverified.",
        description: "Useful for a real request batch; it is not bitwise-identical to fold scheduling."
      }, {
        id: "fold",
        label: "Fold",
        flags: ["--encoder-parallel fold"],
        disabled: s => s.nodes > 1,
        disableReason: "Fold assumes fast node-local peer-to-peer access.",
        description: "Uses one folded encoder copy across a node-local group and preserves native weights."
      }, {
        id: "replicate",
        label: "Replicate",
        flags: ["--encoder-parallel replicate"],
        recommendedWhen: s => s.nodes > 1,
        description: "The safe cross-node default because encoder auto is not node-boundary aware."
      }]
    }, {
      id: "execution",
      title: "Execution",
      scope: "serve",
      description: "Choose eager execution or the measured breakable CUDA graph path.",
      quality: "Graph policy",
      learnMore: "#7-feature-contracts-and-advanced-recipes",
      default: "eager",
      options: [{
        id: "eager",
        label: "Eager",
        recommended: true,
        description: "Reference execution and the consistency baseline."
      }, {
        id: "bcg",
        label: "Compatible BCG",
        flags: ["--enable-breakable-cuda-graph true", "--warmup-resolutions 1344x768", "--bcg-text-buckets 5504"],
        soft: s => !(["b200", "h200"].includes(s.hw) && s.weights === "ref2va"),
        softReason: "Verified for B200/H200 Ref2VA; BCG runs on the other recipes but they have not been through a verification round yet.",
        description: "Reuses matching execution signatures and reserves capture memory; it takes precedence over Cache-DiT."
      }]
    }, {
      id: "quality",
      title: "Quality",
      scope: "request",
      docsHref: "/docs/sglang-diffusion/cache_dit",
      description: "Cumulative reference, fusion-only, or audited Cache-DiT execution.",
      quality: "Sampling policy",
      learnMore: "#choose-the-quality-level",
      default: "lossless",
      options: [{
        id: "lossless",
        label: "Lossless",
        recommended: true,
        description: "Reference-exact denoising without Cache-DiT approximation."
      }, {
        id: "extra-high",
        label: "Extra high",
        description: "Includes fusion-only request paths but not Cache-DiT; MiniMax-H3 currently follows its lossless denoise path at this tier."
      }, {
        id: "high",
        label: "Audited high",
        disabled: s => s.execution !== "eager",
        disableReason: "BCG supersedes Cache-DiT, so the preset would have no effect — switch Execution to Eager to use it.",
        soft: s => !(s.hw === "h200" && s.nodes === 1 && s.gpus_per_node === 4 && ["auto", "resident"].includes(s.placement)),
        softReason: "The 1.40× / SSIM 0.931 audit covers the resident eager 4× H200 workload; elsewhere the preset runs but its quality figures are unaudited.",
        description: "Measured 1.40× with SSIM 0.931 and PSNR 28.16 dB on the audited workload."
      }]
    }, {
      id: "outputs",
      title: "Outputs",
      scope: "request",
      description: "Generate independent variants from one request.",
      quality: "1–10",
      kind: "number",
      min: 1,
      max: 10,
      unit: "outputs per prompt",
      default: 1,
      options: []
    }],
    commandBuilder: {
      defaultSelection: {
        hw: "b200",
        nodes: 1,
        gpus_per_node: 8,
        topology_mode: "auto",
        tp_size: 1,
        ulysses_degree: 8,
        ring_degree: 1
      },
      resource: {
        limits: {
          nodes: {
            min: 1,
            max: 8
          },
          gpus_per_node: {
            min: 1,
            max: 8
          }
        },
        verifiedRecipes: [{
          id: "b200-resident-8",
          hw: "b200",
          nodes: 1,
          gpus_per_node: 8,
          placement: "resident",
          tp_size: 1,
          ulysses_degree: 8,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "b200-fsdp-4",
          hw: "b200",
          nodes: 1,
          gpus_per_node: 4,
          placement: "fsdp",
          tp_size: 1,
          ulysses_degree: 4,
          ring_degree: 1,
          encoder: "auto"
        }, {
          id: "b300-resident-8",
          hw: "b300",
          nodes: 1,
          gpus_per_node: 8,
          placement: "resident",
          tp_size: 1,
          ulysses_degree: 8,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "b300-fsdp-8",
          hw: "b300",
          nodes: 1,
          gpus_per_node: 8,
          placement: "fsdp",
          tp_size: 1,
          ulysses_degree: 8,
          ring_degree: 1,
          encoder: "auto"
        }, {
          id: "gb300-resident-4",
          hw: "gb300",
          nodes: 1,
          gpus_per_node: 4,
          placement: "resident",
          tp_size: 1,
          ulysses_degree: 4,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "gb200-resident-4",
          hw: "gb200",
          nodes: 1,
          gpus_per_node: 4,
          placement: "resident",
          tp_size: 1,
          ulysses_degree: 4,
          ring_degree: 1,
          encoder: "auto",
          default: true,
          unverified: true
        }, {
          id: "h200-resident-4",
          hw: "h200",
          nodes: 1,
          gpus_per_node: 4,
          placement: "resident",
          tp_size: 1,
          ulysses_degree: 4,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "h200-fsdp-4",
          hw: "h200",
          nodes: 1,
          gpus_per_node: 4,
          placement: "fsdp",
          tp_size: 1,
          ulysses_degree: 4,
          ring_degree: 1,
          encoder: "auto"
        }, {
          id: "h200-cross-node-16",
          hw: "h200",
          nodes: 2,
          gpus_per_node: 8,
          placement: "resident",
          tp_size: 1,
          ulysses_degree: 8,
          ring_degree: 2,
          encoder: "replicate"
        }, {
          id: "h100-resident-4",
          hw: "h100",
          nodes: 1,
          gpus_per_node: 4,
          placement: "resident",
          tp_size: 2,
          ulysses_degree: 2,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "h100-fsdp-4",
          hw: "h100",
          nodes: 1,
          gpus_per_node: 4,
          placement: "fsdp",
          tp_size: 1,
          ulysses_degree: 4,
          ring_degree: 1,
          encoder: "auto"
        }, {
          id: "mi300x-resident-1",
          hw: "mi300x",
          nodes: 1,
          gpus_per_node: 1,
          placement: "resident",
          tp_size: 1,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto"
        }, {
          id: "mi300x-resident-2",
          hw: "mi300x",
          nodes: 1,
          gpus_per_node: 2,
          placement: "resident",
          tp_size: 1,
          ulysses_degree: 2,
          ring_degree: 1,
          encoder: "auto"
        }, {
          id: "mi300x-resident-4",
          hw: "mi300x",
          nodes: 1,
          gpus_per_node: 4,
          placement: "resident",
          tp_size: 1,
          ulysses_degree: 4,
          ring_degree: 1,
          encoder: "auto"
        }, {
          id: "mi300x-resident-8",
          hw: "mi300x",
          nodes: 1,
          gpus_per_node: 8,
          placement: "resident",
          tp_size: 1,
          ulysses_degree: 8,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "mi355x-resident-1",
          hw: "mi355x",
          nodes: 1,
          gpus_per_node: 1,
          placement: "resident",
          tp_size: 1,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto"
        }, {
          id: "mi355x-resident-2",
          hw: "mi355x",
          nodes: 1,
          gpus_per_node: 2,
          placement: "resident",
          tp_size: 1,
          ulysses_degree: 2,
          ring_degree: 1,
          encoder: "auto"
        }, {
          id: "mi355x-resident-4",
          hw: "mi355x",
          nodes: 1,
          gpus_per_node: 4,
          placement: "resident",
          tp_size: 1,
          ulysses_degree: 4,
          ring_degree: 1,
          encoder: "auto"
        }, {
          id: "mi355x-resident-8",
          hw: "mi355x",
          nodes: 1,
          gpus_per_node: 8,
          placement: "resident",
          tp_size: 1,
          ulysses_degree: 8,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "dgx-spark-offload-1",
          hw: "dgx-spark",
          nodes: 1,
          gpus_per_node: 1,
          placement: "offload",
          tp_size: 1,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto",
          default: true,
          unverified: true
        }, {
          id: "rtxpro6000-offload-1",
          hw: "rtxpro6000",
          nodes: 1,
          gpus_per_node: 1,
          placement: "offload",
          tp_size: 1,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto",
          default: true,
          unverified: true
        }, {
          id: "rtx6000ada-offload-1",
          hw: "rtx6000ada",
          nodes: 1,
          gpus_per_node: 1,
          placement: "offload",
          tp_size: 1,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto",
          default: true,
          unverified: true
        }, {
          id: "rtx5090-offload-1",
          hw: "rtx5090",
          nodes: 1,
          gpus_per_node: 1,
          placement: "offload",
          tp_size: 1,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "rtx5090-offload-2",
          hw: "rtx5090",
          nodes: 1,
          gpus_per_node: 2,
          placement: "offload",
          tp_size: 2,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto"
        }, {
          id: "rtx4090-offload-1",
          hw: "rtx4090",
          nodes: 1,
          gpus_per_node: 1,
          placement: "offload",
          tp_size: 1,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "rtx4080-offload-1",
          hw: "rtx4080",
          nodes: 1,
          gpus_per_node: 1,
          placement: "offload",
          tp_size: 1,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "rtx3090-offload-1",
          hw: "rtx3090",
          nodes: 1,
          gpus_per_node: 1,
          placement: "offload",
          tp_size: 1,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "rtx5080-offload-1",
          hw: "rtx5080",
          nodes: 1,
          gpus_per_node: 1,
          placement: "offload",
          tp_size: 1,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "rtx5070ti-offload-1",
          hw: "rtx5070ti",
          nodes: 1,
          gpus_per_node: 1,
          placement: "offload",
          tp_size: 1,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "rtx4060ti-offload-1",
          hw: "rtx4060ti",
          nodes: 1,
          gpus_per_node: 1,
          placement: "offload",
          tp_size: 1,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "rtx5070-offload-1",
          hw: "rtx5070",
          nodes: 1,
          gpus_per_node: 1,
          placement: "offload",
          tp_size: 1,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "rtx3060-offload-1",
          hw: "rtx3060",
          nodes: 1,
          gpus_per_node: 1,
          placement: "offload",
          tp_size: 1,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }, {
          id: "rtx4070-offload-1",
          hw: "rtx4070",
          nodes: 1,
          gpus_per_node: 1,
          placement: "offload",
          tp_size: 1,
          ulysses_degree: 1,
          ring_degree: 1,
          encoder: "auto",
          default: true
        }],
        autoTopology: s => {
          const recipes = config.commandBuilder.resource.verifiedRecipes;
          const exact = recipes.find(recipe => recipe.hw === s.hw && recipe.nodes === Number(s.nodes) && recipe.gpus_per_node === Number(s.gpus_per_node) && (s.placement === "auto" || recipe.placement === s.placement));
          if (exact) {
            return {
              tp_size: exact.tp_size,
              ulysses_degree: exact.ulysses_degree,
              ring_degree: exact.ring_degree
            };
          }
          return {
            tp_size: 1,
            ulysses_degree: Number(s.gpus_per_node),
            ring_degree: Number(s.nodes)
          };
        },
        validateTopology: (s, topology) => {
          const errors = [];
          const nodes = Number(s.nodes);
          const perNode = Number(s.gpus_per_node);
          const world = nodes * perNode;
          const tp = Number(topology.tp_size);
          const ulysses = Number(topology.ulysses_degree);
          const ring = Number(topology.ring_degree);
          if (!Number.isInteger(nodes) || nodes < 1 || nodes > 8) errors.push("H3 supports 1–8 nodes.");
          if (!Number.isInteger(perNode) || perNode < 1 || perNode > 8) errors.push("H3 supports 1–8 GPUs per node.");
          if (![1, 2, 4, 8].includes(tp)) errors.push("Tensor parallel size must be one of 1, 2, 4, or 8.");
          if (world !== tp * ulysses * ring) errors.push(`World size ${world} must equal TP × Ulysses × Ring (${tp * ulysses * ring}).`);
          if (56 % tp !== 0 || 56 / tp % ulysses !== 0) errors.push("H3's 56 attention heads must divide evenly across TP and Ulysses.");
          if (64 % (ulysses * ring) !== 0) errors.push("Ulysses × Ring must divide the 64 packed sequence partitions.");
          return errors;
        }
      },
      resolveDeployment: s => {
        const resource = config.commandBuilder.resource;
        const topology = s.topology_mode === "manual" ? {
          tp_size: Number(s.tp_size),
          ulysses_degree: Number(s.ulysses_degree),
          ring_degree: Number(s.ring_degree)
        } : resource.autoTopology(s);
        const errors = resource.validateTopology(s, topology);
        const automaticRecipe = resource.verifiedRecipes.find(entry => entry.hw === s.hw && entry.nodes === Number(s.nodes) && entry.gpus_per_node === Number(s.gpus_per_node) && entry.tp_size === topology.tp_size && entry.ulysses_degree === topology.ulysses_degree && entry.ring_degree === topology.ring_degree);
        const resolvedPlacement = s.placement === "auto" ? automaticRecipe?.placement || (s.hw === "rtx5090" || CONSUMER_SINGLE.includes(s.hw) ? "offload" : "resident") : s.placement;
        const coverageWarnings = [];
        if (resolvedPlacement === "offload" && s.hw !== "rtx5090" && !CONSUMER_SINGLE.includes(s.hw)) {
          coverageWarnings.push("Layerwise offload is tuned and verified on consumer cards; on this hardware it runs unverified and a resident recipe is faster.");
        }
        if (CONSUMER_SINGLE.includes(s.hw) && s.host_ram === "ram64") {
          coverageWarnings.push("48-64 GB hosts sit between the measured 32 GB and 96 GB points and have not been through their own verification round.");
        }
        if (resolvedPlacement === "fsdp" && (s.nodes !== 1 || !["b200", "b300", "h200", "h100"].includes(s.hw))) {
          coverageWarnings.push("FSDP outside the single-node NVIDIA recipes runs unverified.");
        }
        if (s.precision === "fp8" && (!["b200", "b300"].includes(s.hw) || resolvedPlacement !== "resident" || s.nodes !== 1)) {
          coverageWarnings.push("Online FP8 outside resident single-node B200/B300 runs unverified.");
        }
        const highAudited = s.hw === "h200" && s.nodes === 1 && s.gpus_per_node === 4 && resolvedPlacement === "resident";
        if (s.quality === "high" && s.execution !== "eager") {
          coverageWarnings.push("BCG supersedes Cache-DiT, so the high preset has no effect under this execution mode.");
        } else if (s.quality === "high" && !highAudited) {
          coverageWarnings.push("The high preset's 1.40× / SSIM 0.931 figures were audited on resident eager 4× H200; this workload is unaudited.");
        }
        const recipe = resource.verifiedRecipes.find(entry => entry.hw === s.hw && entry.nodes === Number(s.nodes) && entry.gpus_per_node === Number(s.gpus_per_node) && entry.placement === resolvedPlacement && entry.tp_size === topology.tp_size && entry.ulysses_degree === topology.ulysses_degree && entry.ring_degree === topology.ring_degree);
        const topologyVerified = !!recipe && !recipe.unverified && errors.length === 0;
        const encoderVerified = s.encoder === "auto" || s.encoder === recipe?.encoder || s.nodes > 1 && s.encoder === "replicate";
        const attentionVerified = s.attention === "platform";
        const precisionVerified = s.precision === "native" || s.precision === "fp8" && ["b200", "b300"].includes(s.hw);
        const executionVerified = s.execution === "eager" || s.execution === "bcg" && ["b200", "h200"].includes(s.hw) && s.weights === "ref2va";
        const checkpointVerified = s.hw !== "gb300" || s.weights === "fl2va";
        const serveVerified = topologyVerified && encoderVerified && attentionVerified && precisionVerified && executionVerified && checkpointVerified;
        const requestCovered = s.hw !== "gb300" || serveVerified && s.weights === "fl2va" && s.mode === "t2va" && s.quality === "lossless" && Number(s.outputs) === 1;
        const requestVerified = topologyVerified && requestCovered && (["lossless", "extra-high"].includes(s.quality) || s.quality === "high" && highAudited && s.execution === "eager");
        const topologyParts = [];
        if (topology.tp_size > 1) topologyParts.push(`TP ${topology.tp_size}`);
        if (Number(s.nodes) > 1) {
          topologyParts.push(`Ulysses ${topology.ulysses_degree} inside each node`);
          topologyParts.push(`Ring ${topology.ring_degree} across nodes`);
        } else {
          topologyParts.push(`Ulysses ${topology.ulysses_degree}`);
        }
        topologyParts.push(({
          resident: "Resident",
          fsdp: "FSDP",
          offload: "Layerwise offload"
        })[resolvedPlacement]);
        topologyParts.push(Number(s.nodes) > 1 ? `${s.nodes} nodes` : "Single node");
        const world = Number(s.nodes) * Number(s.gpus_per_node);
        const flags = ["--model-path {{MODEL_NAME}}"];
        if (world > 1) flags.push(`--num-gpus ${world}`);
        if (topology.ring_degree > 1) flags.push(`--sp-degree ${world}`);
        if (topology.tp_size > 1) flags.push(`--tp-size ${topology.tp_size}`);
        if (topology.ulysses_degree > 1) flags.push(`--ulysses-degree ${topology.ulysses_degree}`);
        if (topology.ring_degree > 1) flags.push(`--ring-degree ${topology.ring_degree}`);
        flags.push("--host {{HOST_IP}}", "--port {{PORT}}");
        const warnings = [...coverageWarnings];
        if (!topologyVerified && errors.length === 0) {
          warnings.push("This topology satisfies H3's static constraints but has not completed an exact end-to-end verification run.");
        }
        if (resolvedPlacement === "fsdp") {
          warnings.push("FSDP lowers resident DiT memory but adds per-block parameter collectives; prefer Resident when the pipeline fits.");
        }
        if (s.hw === "rtx5090" && Number(s.gpus_per_node) === 2) {
          warnings.push("The 2× RTX 5090 path requires a 384 GiB-class host and prioritizes capacity over latency.");
        }
        let automaticAttention = "FlashAttention (auto)";
        if (["mi300x", "mi355x"].includes(s.hw)) {
          automaticAttention = "AITER (auto)";
        } else if (topology.ring_degree === 1 && ["b200", "b300", "gb200", "gb300"].includes(s.hw)) {
          automaticAttention = "Dynamic cuDNN / FA (auto)";
        } else if (topology.ring_degree === 1 && ["rtx5090", "rtx4090"].includes(s.hw)) {
          automaticAttention = "Torch SDPA (auto)";
        }
        return {
          match: {
            hw: s.hw
          },
          nnodes: Number(s.nodes),
          verified: serveVerified,
          verificationStatus: serveVerified ? "verified" : "unverified",
          flags,
          builder: {
            topology,
            topologySummary: topologyParts.filter(Boolean).join(" · "),
            errors,
            warnings,
            verification: {
              serve: errors.length ? "error" : serveVerified ? "verified" : "unverified",
              request: errors.length ? "error" : requestVerified ? "verified" : "unverified"
            },
            resolvedSettings: {
              placement: ({
                resident: "Resident",
                fsdp: "FSDP",
                offload: "Layerwise offload"
              })[resolvedPlacement],
              attention: s.attention === "platform" ? automaticAttention : undefined,
              encoder: s.encoder === "auto" ? s.nodes > 1 ? "Replicate (auto)" : "Auto" : undefined
            }
          }
        };
      }
    },
    modelNames: {
      default: "MiniMaxAI/MiniMax-H3"
    },
    placeholders: {
      HOST_IP: {
        target: "command",
        label: "Bind host",
        default: "0.0.0.0"
      },
      PORT: {
        target: "command",
        label: "Bind port",
        default: "30010"
      },
      HF_TOKEN: {
        target: "command",
        label: "HF token (Docker)",
        default: "<your-hf-token>"
      },
      MEDIA_DIR: {
        target: "command",
        label: "Host media directory (Docker)",
        default: "/data/minimax-h3"
      },
      CURL_HOST: {
        target: "curl",
        label: "Server host",
        default: "localhost"
      },
      CURL_PORT: {
        target: "curl",
        label: "Server port",
        default: "30010"
      },
      DURATION_SECONDS: {
        target: "curl",
        label: "Duration (seconds, 4-15)",
        default: "5"
      },
      FIRST_FRAME: {
        target: "curl",
        label: "FL2VA first frame URI",
        default: "file:///data/minimax-h3/first-frame.png"
      },
      LAST_FRAME: {
        target: "curl",
        label: "FL2VA last frame URI",
        default: "file:///data/minimax-h3/last-frame.png"
      },
      INPUT_VIDEO: {
        target: "curl",
        label: "First video URI",
        default: "file:///data/minimax-h3/video-1.mp4"
      },
      INPUT_VIDEO_START_SECONDS: {
        target: "curl",
        label: "First video start (seconds)",
        default: "0"
      },
      SECOND_INPUT_VIDEO: {
        target: "curl",
        label: "Second video URI (mixed ref)",
        default: "file:///data/minimax-h3/video-2.mp4"
      },
      SECOND_INPUT_VIDEO_START_SECONDS: {
        target: "curl",
        label: "Second video start (seconds)",
        default: "0"
      },
      REFERENCE_IMAGE: {
        target: "curl",
        label: "First reference image URI",
        default: "file:///data/minimax-h3/reference-1.png"
      },
      SECOND_REFERENCE_IMAGE: {
        target: "curl",
        label: "Second reference image URI",
        default: "file:///data/minimax-h3/reference-2.png"
      },
      REFERENCE_AUDIO: {
        target: "curl",
        label: "First reference audio URI",
        default: "file:///data/minimax-h3/reference-1.mp3"
      },
      SECOND_REFERENCE_AUDIO: {
        target: "curl",
        label: "Second reference audio URI",
        default: "file:///data/minimax-h3/reference-2.mp3"
      }
    },
    curl: s => {
      const request = {
        model: "{{MODEL_NAME}}",
        prompt: "Night-vision bedroom footage: while the owner sleeps, three cats burst in playing tiny brass instruments at full volume, freeze, then march out as if nothing happened.",
        seconds: "{{DURATION_SECONDS}}",
        task: "t2va",
        conditions: [],
        target: {
          short_edge: 768,
          aspect_ratio: "16:9",
          duration_seconds: "{{DURATION_SECONDS}}"
        },
        quality: s.quality,
        num_outputs_per_prompt: Number(s.outputs),
        num_inference_steps: 50,
        flow_shift: 12.0,
        audio_flow_shift: 3.0,
        seed: 1101
      };
      const imageReference = uri => ({
        type: "image",
        uri,
        role: "reference"
      });
      const audioReference = uri => ({
        type: "audio",
        uri,
        role: "reference"
      });
      const videoReference = (uri, start, type = "video") => ({
        type,
        uri,
        role: "reference",
        start_time_seconds: start
      });
      if (["i2va", "l2va", "fl2va"].includes(s.mode)) {
        request.task = "fl2va";
        request.prompt = "Continue naturally between the supplied endpoint frame or frames, with synchronized ambient sound.";
        request.target.aspect_ratio = "auto";
        request.seed = 2101;
        request.conditions = [];
        if (s.mode !== "l2va") {
          request.conditions.push({
            type: "image",
            uri: "{{FIRST_FRAME}}",
            role: "keyframe",
            frame_index: 0
          });
        }
        if (s.mode !== "i2va") {
          request.conditions.push({
            type: "image",
            uri: "{{LAST_FRAME}}",
            role: "keyframe",
            frame_index: -1
          });
        }
      } else if (s.mode === "ref_image") {
        request.task = "ref2va";
        request.prompt = "Use <Picture 1> as the visual subject and style reference.";
        request.target.aspect_ratio = "auto";
        request.conditions = [imageReference("{{REFERENCE_IMAGE}}")];
        request.seed = 3101;
      } else if (s.mode === "ref_image_audio") {
        request.task = "ref2va";
        request.prompt = "Use <Picture 1> as the visual subject and <Audio 1> as the sound reference.";
        request.target.aspect_ratio = "auto";
        request.conditions = [imageReference("{{REFERENCE_IMAGE}}"), audioReference("{{REFERENCE_AUDIO}}")];
        request.seed = 3102;
      } else if (s.mode === "v2v" || s.mode === "video_audio") {
        request.task = "ref2va";
        request.prompt = s.mode === "video_audio" ? "Follow <Video 1> and its required <Audio 1> soundtrack with coherent synchronized motion." : "Follow the appearance and motion of <Video 1>; use its soundtrack when present.";
        request.conditions = [videoReference("{{INPUT_VIDEO}}", "{{INPUT_VIDEO_START_SECONDS}}", s.mode === "video_audio" ? "video_audio" : "video")];
        request.seed = s.mode === "video_audio" ? 4102 : 4101;
      } else if (s.mode === "audio_only") {
        request.task = "ref2va";
        request.prompt = "Build a coherent visual scene around <Audio 1>.";
        request.conditions = [audioReference("{{REFERENCE_AUDIO}}")];
        request.seed = 3103;
      } else if (s.mode === "mixed_ref") {
        request.task = "ref2va";
        request.prompt = "Combine <Picture 1>, <Picture 2>, <Audio 1>, <Audio 2>, <Video 1>, and <Video 2> in their one-based modality order.";
        request.conditions = [imageReference("{{REFERENCE_IMAGE}}"), imageReference("{{SECOND_REFERENCE_IMAGE}}"), audioReference("{{REFERENCE_AUDIO}}"), audioReference("{{SECOND_REFERENCE_AUDIO}}"), videoReference("{{INPUT_VIDEO}}", "{{INPUT_VIDEO_START_SECONDS}}"), videoReference("{{SECOND_INPUT_VIDEO}}", "{{SECOND_INPUT_VIDEO_START_SECONDS}}")];
        request.seed = 3104;
      }
      const body = JSON.stringify(request, null, 2).replace(/"{{(DURATION_SECONDS|INPUT_VIDEO_START_SECONDS|SECOND_INPUT_VIDEO_START_SECONDS)}}"/g, "{{$1}}");
      return `curl -sS -X POST http://{{CURL_HOST}}:{{CURL_PORT}}/v1/videos \\
  -H 'Content-Type: application/json' \\
  -d '${body}'`;
    },
    dockerMounts: ["{{MEDIA_DIR}}:/data/minimax-h3:ro"],
    dockerRunCommand: s => ["mi300x", "mi355x"].includes(s.hw) ? `bash -lc 'python -m pip install -e "/sgl-workspace/sglang/python[diffusion_hip]" && exec sglang serve "$@"' --` : `bash -lc 'python -m pip install -e "/sgl-workspace/sglang/python[diffusion]" && exec sglang serve "$@"' --`,
    runModes: s => ["mi300x", "mi355x", "gb200", "gb300"].includes(s.hw) ? ["python"] : ["python", "docker"],
    dockerImages: {
      b200: "lmsysorg/sglang:dev",
      b300: "lmsysorg/sglang:dev",
      h200: "lmsysorg/sglang:dev",
      h100: "lmsysorg/sglang:dev"
    },
    showPlaygroundLink: false,
    cells: []
  };
})();

export const Deployment = ({config, benchmarks}) => {
  if (!config) {
    return <div style={{
      padding: 12,
      color: "#b91c1c"
    }}>Deployment: missing <code>config</code> prop</div>;
  }
  const AMD_RDMA_DOCKER_FLAGS = ["--device /dev/infiniband", "--cap-add IPC_LOCK", "--ulimit memlock=-1", "--ulimit stack=67108864", "--ulimit nofile=1048576:1048576"];
  const HARDWARE_CATALOG = {
    blackwell: [{
      id: "b300",
      label: "B300",
      vram: "288GB"
    }, {
      id: "gb300",
      label: "GB300",
      vram: "288GB"
    }, {
      id: "b200",
      label: "B200",
      vram: "192GB"
    }, {
      id: "gb200",
      label: "GB200",
      vram: "192GB"
    }, {
      id: "dgx-spark",
      label: "DGX Spark",
      vram: "128GB",
      multiNodeDockerFlags: ["--ulimit memlock=-1:-1", "--cap-add IPC_LOCK", "--device /dev/infiniband"]
    }],
    hopper: [{
      id: "h200",
      label: "H200",
      vram: "141GB"
    }, {
      id: "h100",
      label: "H100",
      vram: "80GB"
    }, {
      id: "h20-3e",
      label: "H20-3e",
      vram: "141GB"
    }, {
      id: "h800",
      label: "H800",
      vram: "80GB"
    }],
    amd: [{
      id: "mi300x",
      label: "MI300X",
      vram: "192GB",
      multiNodeDockerFlags: [...AMD_RDMA_DOCKER_FLAGS]
    }, {
      id: "mi325x",
      label: "MI325X",
      vram: "256GB",
      multiNodeDockerFlags: [...AMD_RDMA_DOCKER_FLAGS]
    }, {
      id: "mi350x",
      label: "MI350X",
      vram: "288GB",
      multiNodeDockerFlags: [...AMD_RDMA_DOCKER_FLAGS]
    }, {
      id: "mi355x",
      label: "MI355X",
      vram: "288GB",
      multiNodeDockerFlags: [...AMD_RDMA_DOCKER_FLAGS]
    }],
    npu: [{
      id: "a3",
      label: "A3 Series",
      vram: "64GB/die",
      npuDevices: 16
    }, {
      id: "a5",
      label: "950PR/DT Series",
      vram: "128GB",
      npuDevices: 8
    }]
  };
  const makeStyles = isDark => ({
    container: {
      maxWidth: "900px",
      margin: "0 auto",
      display: "flex",
      flexDirection: "column",
      gap: "3px"
    },
    card: {
      padding: "5px 10px",
      border: `1px solid ${isDark ? "#374151" : "#e5e7eb"}`,
      borderLeft: `3px solid ${isDark ? "#E85D4D" : "#D45D44"}`,
      borderRadius: "4px",
      display: "flex",
      alignItems: "center",
      gap: "10px",
      background: isDark ? "#1f2937" : "#fff"
    },
    cardColumn: {
      padding: "5px 10px",
      border: `1px solid ${isDark ? "#374151" : "#e5e7eb"}`,
      borderLeft: `3px solid ${isDark ? "#E85D4D" : "#D45D44"}`,
      borderRadius: "4px",
      display: "flex",
      flexDirection: "column",
      gap: "4px",
      background: isDark ? "#1f2937" : "#fff"
    },
    title: {
      fontSize: "12px",
      fontWeight: "600",
      minWidth: "108px",
      flexShrink: 0,
      color: isDark ? "#e5e7eb" : "inherit"
    },
    vendorRow: {
      display: "flex",
      alignItems: "center",
      gap: "6px"
    },
    vendorLabel: {
      fontSize: "10px",
      fontWeight: "600",
      color: isDark ? "#9ca3af" : "#6b7280",
      width: "68px",
      flexShrink: 0,
      textTransform: "uppercase",
      letterSpacing: "0.04em"
    },
    itemsGrid: () => ({
      display: "grid",
      gridTemplateColumns: "repeat(auto-fit, minmax(72px, 1fr))",
      gap: "4px",
      flex: 1
    }),
    labelBase: {
      padding: "2px 8px",
      border: `1px solid ${isDark ? "#9ca3af" : "#d1d5db"}`,
      borderRadius: "3px",
      cursor: "pointer",
      display: "inline-flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      fontWeight: "500",
      fontSize: "12px",
      transition: "all 0.2s",
      userSelect: "none",
      minHeight: "26px",
      textAlign: "center",
      background: isDark ? "#374151" : "#fff",
      color: isDark ? "#e5e7eb" : "inherit"
    },
    checked: {
      background: "#D45D44",
      color: "white",
      borderColor: "#D45D44"
    },
    disabled: {
      cursor: "not-allowed",
      opacity: 0.4
    },
    subtitle: {
      display: "block",
      fontSize: "9px",
      marginTop: "1px",
      lineHeight: "1.1",
      opacity: 0.7
    },
    commandWrap: {
      position: "relative",
      flex: 1,
      background: isDark ? "#111827" : "#f5f5f5",
      borderRadius: "6px",
      border: `1px solid ${isDark ? "#374151" : "#e5e7eb"}`,
      overflow: "hidden"
    },
    commandHeader: {
      display: "flex",
      flexWrap: "wrap",
      justifyContent: "space-between",
      alignItems: "center",
      gap: "6px 10px",
      padding: "6px 10px",
      borderBottom: `1px solid ${isDark ? "#374151" : "#e5e7eb"}`,
      background: isDark ? "#1f2937" : "#fafafa"
    },
    commandPre: {
      padding: "12px 16px",
      fontFamily: "'Menlo', 'Monaco', 'Courier New', monospace",
      fontSize: "12px",
      lineHeight: "1.5",
      color: isDark ? "#e5e7eb" : "#374151",
      whiteSpace: "pre-wrap",
      overflowX: "auto",
      margin: 0
    },
    mtpWarn: {
      margin: "8px 0 0",
      padding: "8px 12px",
      borderRadius: "8px",
      fontSize: "12px",
      lineHeight: "1.45",
      background: isDark ? "#78350f" : "#fef3c7",
      color: isDark ? "#fde68a" : "#92400e",
      border: `1px solid ${isDark ? "#92400e" : "#fcd34d"}`
    },
    badge: status => ({
      display: "inline-flex",
      alignItems: "center",
      gap: "6px",
      padding: "2px 8px",
      borderRadius: "10px",
      background: ({
        verified: isDark ? "#064e3b" : "#d1fae5",
        "in-progress": isDark ? "#1e3a8a" : "#dbeafe",
        unverified: isDark ? "#78350f" : "#fef3c7"
      })[verifyStatusOf(status)],
      color: ({
        verified: isDark ? "#a7f3d0" : "#065f46",
        "in-progress": isDark ? "#bfdbfe" : "#1e40af",
        unverified: isDark ? "#fde68a" : "#92400e"
      })[verifyStatusOf(status)],
      fontSize: "11px",
      fontWeight: 600,
      whiteSpace: "nowrap"
    }),
    badgeDot: status => ({
      width: "8px",
      height: "8px",
      borderRadius: "50%",
      background: ({
        verified: "#10b981",
        "in-progress": "#3b82f6",
        unverified: "#f59e0b"
      })[verifyStatusOf(status)]
    }),
    iconButton: {
      padding: "4px 10px",
      border: `1px solid ${isDark ? "#4b5563" : "#d1d5db"}`,
      borderRadius: "4px",
      background: isDark ? "#1f2937" : "#fff",
      color: isDark ? "#e5e7eb" : "#374151",
      fontSize: "11px",
      fontWeight: 500,
      cursor: "pointer",
      display: "inline-flex",
      alignItems: "center",
      gap: "4px"
    },
    iconRow: {
      display: "inline-flex",
      flexWrap: "wrap",
      gap: "6px"
    },
    runModeWrap: {
      display: "inline-flex",
      border: `1px solid ${isDark ? "#4b5563" : "#d1d5db"}`,
      borderRadius: "10px",
      overflow: "hidden",
      fontSize: "11px",
      fontWeight: 600,
      userSelect: "none"
    },
    runModeChip: active => ({
      padding: "2px 10px",
      cursor: "pointer",
      background: active ? isDark ? "#1f2937" : "#fff" : "transparent",
      color: active ? isDark ? "#e5e7eb" : "#111827" : isDark ? "#9ca3af" : "#6b7280",
      borderRight: `1px solid ${isDark ? "#4b5563" : "#d1d5db"}`
    }),
    runModeChipLast: active => ({
      padding: "2px 10px",
      cursor: "pointer",
      background: active ? isDark ? "#1f2937" : "#fff" : "transparent",
      color: active ? isDark ? "#e5e7eb" : "#111827" : isDark ? "#9ca3af" : "#6b7280"
    }),
    headerLeft: {
      display: "inline-flex",
      flexWrap: "wrap",
      alignItems: "center",
      gap: "8px"
    },
    modalBackdrop: {
      position: "fixed",
      inset: 0,
      background: "rgba(0,0,0,0.5)",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      zIndex: 9999
    },
    modalBox: {
      background: isDark ? "#1f2937" : "#fff",
      color: isDark ? "#e5e7eb" : "#111827",
      borderRadius: "8px",
      padding: "20px",
      maxWidth: "720px",
      width: "92%",
      maxHeight: "85vh",
      overflowY: "auto",
      border: `1px solid ${isDark ? "#374151" : "#e5e7eb"}`,
      boxShadow: "0 10px 25px rgba(0,0,0,0.25)"
    },
    modalHeader: {
      display: "flex",
      justifyContent: "space-between",
      alignItems: "center",
      marginBottom: "12px"
    },
    modalTitle: {
      fontSize: "15px",
      fontWeight: 600
    },
    modalCloseBtn: {
      background: "transparent",
      border: "none",
      color: "inherit",
      fontSize: "20px",
      cursor: "pointer",
      padding: "0 6px",
      lineHeight: 1
    },
    formField: {
      display: "flex",
      flexDirection: "column",
      gap: "4px",
      marginBottom: "10px"
    },
    formLabel: {
      fontSize: "12px",
      fontWeight: 500,
      color: isDark ? "#9ca3af" : "#4b5563"
    },
    formInput: {
      padding: "6px 10px",
      fontSize: "13px",
      border: `1px solid ${isDark ? "#4b5563" : "#d1d5db"}`,
      borderRadius: "4px",
      background: isDark ? "#111827" : "#fff",
      color: isDark ? "#e5e7eb" : "#111827",
      fontFamily: "'Menlo', 'Monaco', 'Courier New', monospace"
    },
    sectionHeading: {
      fontSize: "12px",
      fontWeight: 600,
      textTransform: "uppercase",
      letterSpacing: "0.04em",
      color: isDark ? "#9ca3af" : "#6b7280",
      margin: "12px 0 6px 0"
    },
    primaryBtn: {
      padding: "6px 14px",
      background: "#D45D44",
      color: "white",
      border: "none",
      borderRadius: "4px",
      cursor: "pointer",
      fontSize: "13px",
      fontWeight: 500
    },
    benchCard: {
      padding: "8px 12px",
      border: `1px solid ${isDark ? "#374151" : "#e5e7eb"}`,
      borderLeft: `3px solid ${isDark ? "#E85D4D" : "#D45D44"}`,
      borderRadius: "4px",
      background: isDark ? "#1f2937" : "#fff",
      display: "flex",
      flexDirection: "column",
      gap: "8px"
    },
    benchHeader: {
      display: "flex",
      flexWrap: "wrap",
      alignItems: "baseline",
      justifyContent: "space-between",
      gap: "6px 12px"
    },
    benchTitle: {
      fontSize: "13px",
      fontWeight: 600,
      color: isDark ? "#e5e7eb" : "inherit"
    },
    benchVersion: {
      fontSize: "11px",
      color: isDark ? "#9ca3af" : "#6b7280"
    },
    benchHeaderRight: {
      display: "flex",
      flexWrap: "wrap",
      alignItems: "center",
      gap: "6px 10px",
      flexShrink: 0
    },
    benchChipRow: {
      display: "flex",
      alignItems: "center",
      gap: "6px",
      flexWrap: "wrap",
      margin: "2px 0 8px"
    },
    benchChip: {
      padding: "2px 10px",
      fontSize: "12px",
      cursor: "pointer",
      border: `1px solid ${isDark ? "#4b5563" : "#d1d5db"}`,
      borderRadius: "4px",
      background: isDark ? "#1f2937" : "#fff",
      color: isDark ? "#e5e7eb" : "#374151",
      fontFamily: "'Menlo', 'Monaco', 'Courier New', monospace"
    },
    benchChipActive: {
      background: "#D45D44",
      color: "white",
      borderColor: "#D45D44"
    },
    benchBlock: {
      border: `1px solid ${isDark ? "#374151" : "#e5e7eb"}`,
      borderRadius: "4px",
      padding: "8px 10px",
      background: isDark ? "#111827" : "#fafafa"
    },
    benchBlockTitle: {
      fontSize: "11px",
      fontWeight: 600,
      textTransform: "uppercase",
      letterSpacing: "0.04em",
      color: isDark ? "#9ca3af" : "#6b7280",
      marginBottom: "4px"
    },
    benchWorkload: {
      fontSize: "11px",
      fontStyle: "italic",
      color: isDark ? "#9ca3af" : "#6b7280",
      marginBottom: "6px",
      lineHeight: "1.3"
    },
    benchRow: {
      display: "flex",
      justifyContent: "space-between",
      fontSize: "12px",
      padding: "2px 0"
    },
    benchKey: {
      color: isDark ? "#9ca3af" : "#6b7280"
    },
    benchVal: {
      color: isDark ? "#e5e7eb" : "#111827",
      fontFamily: "'Menlo', 'Monaco', 'Courier New', monospace",
      fontWeight: 500
    },
    benchNotes: {
      fontSize: "11px",
      fontStyle: "italic",
      color: isDark ? "#9ca3af" : "#6b7280"
    },
    benchLegend: {
      fontSize: "10px",
      fontStyle: "italic",
      color: isDark ? "#6b7280" : "#9ca3af",
      marginTop: "6px",
      fontFamily: "'Menlo', 'Monaco', 'Courier New', monospace"
    },
    benchEmpty: {
      fontSize: "12px",
      fontStyle: "italic",
      color: isDark ? "#9ca3af" : "#6b7280"
    },
    benchTableScroll: {
      overflowX: "auto",
      overflowY: "hidden",
      marginTop: "4px"
    },
    benchTable: {
      display: "grid",
      columnGap: 0,
      rowGap: "3px",
      alignItems: "baseline",
      minWidth: "max-content"
    },
    benchTableHead: {
      textAlign: "right",
      fontWeight: 500,
      fontSize: "11px",
      color: isDark ? "#9ca3af" : "#6b7280",
      paddingLeft: "16px",
      paddingBottom: "4px",
      whiteSpace: "nowrap"
    },
    benchTableCornerHead: {
      paddingBottom: "4px",
      position: "sticky",
      left: 0,
      background: isDark ? "#111827" : "#fafafa",
      zIndex: 1,
      alignSelf: "stretch"
    },
    benchTableSeparator: {
      gridColumn: "1 / -1",
      height: "1px",
      background: isDark ? "#374151" : "#e5e7eb",
      marginTop: "-3px"
    },
    benchTableLabel: {
      textAlign: "left",
      fontSize: "12px",
      color: isDark ? "#9ca3af" : "#6b7280",
      whiteSpace: "nowrap",
      position: "sticky",
      left: 0,
      background: isDark ? "#111827" : "#fafafa",
      zIndex: 1
    },
    benchTableValue: {
      textAlign: "right",
      fontSize: "12px",
      color: isDark ? "#e5e7eb" : "#111827",
      fontFamily: "'Menlo', 'Monaco', 'Courier New', monospace",
      fontWeight: 500,
      paddingLeft: "16px",
      whiteSpace: "nowrap"
    },
    benchTableValueMissing: {
      color: isDark ? "#6b7280" : "#9ca3af"
    }
  });
  const VERIFY_LABEL = {
    verified: "Verified",
    "in-progress": "Final Verification In Progress",
    unverified: "Not Verified"
  };
  const verifyStatusOf = v => typeof v === "string" ? VERIFY_LABEL[v] ? v : "unverified" : v ? "verified" : "unverified";
  const cellVerifyStatus = (c, sel) => {
    if (!c) return "unverified";
    const v = typeof c.verificationStatus === "function" ? c.verificationStatus(sel) : c.verificationStatus;
    return verifyStatusOf(v ?? c.verified);
  };
  const LEGACY_MATCH_DIMS = [{
    id: "variant",
    title: "Model Variant",
    optionsKey: "variants"
  }, {
    id: "quant",
    title: "Quantization",
    optionsKey: "quantizations"
  }, {
    id: "strategy",
    title: "Strategy",
    optionsKey: "strategies"
  }, {
    id: "nodes",
    title: "Nodes",
    optionsKey: "nodesOptions"
  }];
  const matchDimSpecs = (config.matchDims || LEGACY_MATCH_DIMS).map(d => ({
    ...d,
    options: d.options || config[d.optionsKey] || []
  }));
  const overlayDimSpecs = config.overlayDims || [];
  const commandBuilder = config.commandBuilder || null;
  const DIMENSIONS = ["hw", ...matchDimSpecs.map(d => d.id)];
  const optionVisible = (opt, sel) => typeof opt.showWhen !== "function" || opt.showWhen(sel);
  const optionDisabled = (opt, sel) => typeof opt.disabled === "function" ? opt.disabled(sel) : !!opt.disabled;
  const visibleOptions = (spec, sel) => (spec.options || []).filter(o => optionVisible(o, sel));
  const rowVisible = (spec, sel) => (typeof spec.showWhen !== "function" || spec.showWhen(sel)) && visibleOptions(spec, sel).length > 0;
  const overlayPick = sel => {
    const picked = [];
    for (const spec of config.overlayDims || []) {
      if (!rowVisible(spec, sel)) continue;
      const opt = (spec.options || []).find(o => o.id === sel[spec.id]);
      if (opt && !optionDisabled(opt, sel)) picked.push(opt);
    }
    return picked;
  };
  const overlayPart = (sel, key) => {
    const out = [];
    for (const opt of overlayPick(sel)) {
      const add = typeof opt[key] === "function" ? opt[key](sel) : opt[key];
      if (add) out.push(...add);
    }
    return out;
  };
  const overlayCompose = (cellFlags, sel) => {
    const strip = overlayPart(sel, "stripPrefixes");
    const add = overlayPart(sel, "flags");
    if (!strip.length) return [...cellFlags || [], ...add];
    const used = new Set();
    const replacementsFor = tok => {
      const out = [];
      add.forEach((f, i) => {
        if (used.has(i) || f.split(/[\s=]/)[0] !== tok) return;
        used.add(i);
        out.push(f);
      });
      return out;
    };
    const out = [];
    for (const f of cellFlags || []) {
      const tok = f.split(/[\s=]/)[0];
      if (!strip.includes(tok)) out.push(f); else out.push(...replacementsFor(tok));
    }
    add.forEach((f, i) => {
      if (!used.has(i)) out.push(f);
    });
    return out;
  };
  const optionSoft = (opt, sel) => typeof opt.soft === "function" ? opt.soft(sel) : !!opt.soft;
  const findCell = (cells, sel) => cells.find(c => DIMENSIONS.every(d => c.match[d] === sel[d]));
  const findBenchmark = (list, sel) => {
    const hits = (list || []).filter(b => Object.entries(b.match || ({})).every(([k, v]) => sel[k] === v));
    return hits.sort((a, b) => Object.keys(b.match).length - Object.keys(a.match).length)[0] || null;
  };
  const normalizeSpeed = speed => {
    if (!speed) return [];
    return Array.isArray(speed) ? speed : [speed];
  };
  const effectiveAccuracy = (entry, sel) => entry ? {
    ...config.defaultAccuracy && config.defaultAccuracy[sel.variant] || ({}),
    ...entry.accuracy || ({})
  } : {};
  const benchmarkIsEmpty = (entry, accuracy) => {
    for (const m of normalizeSpeed(entry && entry.speed)) {
      if (m && typeof m === "object") {
        for (const [key, v] of Object.entries(m)) {
          if (key === "workload") continue;
          if (v !== null && v !== undefined) return false;
        }
      }
    }
    if (accuracy && typeof accuracy === "object") {
      for (const v of Object.values(accuracy)) {
        if (v !== null && v !== undefined) return false;
      }
    }
    return true;
  };
  const isOptionAvailable = (cells, sel, dim, value) => {
    const idx = DIMENSIONS.indexOf(dim);
    const higher = DIMENSIONS.slice(0, idx);
    return cells.some(c => c.match[dim] === value && higher.every(d => c.match[d] === sel[d]));
  };
  const snapToValidCell = (cells, sel, dim, value) => {
    const idx = DIMENSIONS.indexOf(dim);
    const higher = DIMENSIONS.slice(0, idx);
    const lower = DIMENSIONS.slice(idx + 1);
    let best = null, bestLowerMatches = -1;
    for (const c of cells) {
      if (c.match[dim] !== value) continue;
      if (!higher.every(d => c.match[d] === sel[d])) continue;
      let s = 0;
      for (const d of lower) if (c.match[d] === sel[d]) s++;
      if (s > bestLowerMatches) {
        bestLowerMatches = s;
        best = c;
      }
    }
    if (!best) return sel;
    const next = {
      ...sel,
      [dim]: value
    };
    for (const d of lower) next[d] = best.match[d];
    return next;
  };
  const validateSelection = (cells, parsed) => {
    const valid = {};
    for (const dim of DIMENSIONS) {
      const want = parsed[dim];
      const works = cells.some(c => c.match[dim] === want && DIMENSIONS.slice(0, DIMENSIONS.indexOf(dim)).every(d => c.match[d] === valid[d]));
      if (works) {
        valid[dim] = want;
      } else {
        const fallback = cells.find(c => DIMENSIONS.slice(0, DIMENSIONS.indexOf(dim)).every(d => c.match[d] === valid[d]));
        valid[dim] = fallback ? fallback.match[dim] : want;
      }
    }
    for (const spec of overlayDimSpecs) {
      const want = parsed[spec.id];
      const opts = spec.options || [];
      const picked = opts.some(o => o.id === want) ? want : (spec.default ?? (opts[0] && opts[0].id)) ?? "";
      const withPick = {
        ...valid,
        [spec.id]: picked
      };
      const usable = visibleOptions(spec, withPick).filter(o => !optionDisabled(o, withPick));
      valid[spec.id] = usable.some(o => o.id === picked) ? picked : (usable[0] && usable[0].id) ?? picked;
    }
    return valid;
  };
  const resolveModelName = sel => {
    const keys = [`${sel.hw}|${sel.variant}|${sel.quant}`, `${sel.variant}|${sel.quant}`, `${sel.hw}|${sel.quant}`, sel.quant, sel.hw, "default"];
    for (const k of keys) {
      const hit = config.modelNames[k];
      if (hit) return hit;
    }
    return "";
  };
  const interpolate = (text, env, modelName) => text.replace(/{{(\w+)}}/g, (_, key) => key === "MODEL_NAME" ? modelName : env[key] ?? `{{${key}}}`);
  const parseNnodes = id => {
    if (Number.isInteger(id)) return id;
    if ((/^\d+$/).test(id || "")) return parseInt(id, 10);
    if (id === "single") return 1;
    const m = (/^multi-(\d+)$/).exec(id || "");
    return m ? parseInt(m[1], 10) : 1;
  };
  const cellNnodes = (cell, sel) => sel.nodes !== undefined ? parseNnodes(sel.nodes) : cell.nnodes || 1;
  const PD_SERVE_PORTS = {
    prefill: 30000,
    decode: 30100
  };
  const overlayEnv = sel => overlayPart(sel, "env");
  const overlayHints = sel => overlayPart(sel, "hints");
  const renderCommand = (cell, sel, envValues, mode = "python") => {
    if (!cell) return "# No command available for the current selection.";
    const modelName = resolveModelName(sel);
    const nnodes = cellNnodes(cell, sel);
    const multinode = nnodes > 1;
    const cellEnv = [...cell.env || [], ...overlayEnv(sel)];
    const flags = overlayCompose(cell.flags, sel);
    if (multinode) {
      const PARALLELISM_ANCHORS = new Set(["--enable-dp-attention", "--dp-size", "--dp", "--tp-size", "--tp", "--sp-degree", "--ulysses-degree", "--ring-degree"]);
      let i = flags.reduce((last, flag, index) => PARALLELISM_ANCHORS.has(flag.split(/[\s=]/)[0]) ? index : last, -1);
      if (i === -1) i = flags.findIndex(f => f.startsWith("--model-path"));
      flags.splice(i + 1, 0, `--nnodes ${nnodes}`, `--node-rank {{NODE_RANK}}`, `--dist-init-addr {{NODE0_IP}}:20000`);
    }
    const pdServePort = PD_SERVE_PORTS[sel.pdMode];
    if (pdServePort !== undefined) {
      for (let j = 0; j < flags.length; j++) {
        if (flags[j].split(/[\s=]/)[0] === "--port") {
          flags[j] = `--port ${pdServePort}`;
        }
      }
    }
    let cmd;
    if (mode === "docker") {
      const di = config.dockerImages || ({});
      const image = di[`${sel.hw}|${sel.variant}|${sel.quant}`] || di[`${sel.variant}|${sel.quant}`] || di[`${sel.hw}|${sel.quant}|${sel.strategy}`] || di[`${sel.hw}|${sel.quant}`] || di[sel.hw] || "lmsysorg/sglang:dev";
      const dockerRunCommand = typeof config.dockerRunCommand === "function" ? config.dockerRunCommand(sel) : config.dockerRunCommand || "sglang serve";
      const portFlag = flags.find(x => x.split(/[\s=]/)[0] === "--port");
      const servePort = portFlag ? portFlag.slice(("--port").length).trim() : "{{PORT}}";
      const hostNetwork = multinode || typeof config.dockerHostNetworkWhen === "function" && config.dockerHostNetworkWhen(sel, {
        flags,
        env: cellEnv
      });
      const vendorOf = hwId => {
        for (const [vendor, list] of Object.entries(HARDWARE_CATALOG)) {
          if (list.some(h => h.id === hwId)) return vendor;
        }
        const extra = (config.hardware || []).find(h => h.id === hwId);
        return extra && extra.vendor || "nvidia";
      };
      const catalogEntryOf = hwId => {
        const extra = (config.hardware || []).find(h => h.id === hwId);
        if (extra) return extra;
        for (const list of Object.values(HARDWARE_CATALOG)) {
          const hit = list.find(h => h.id === hwId);
          if (hit) return hit;
        }
        return null;
      };
      const fabricFlagsOf = hwId => (catalogEntryOf(hwId) || ({})).multiNodeDockerFlags || [];
      const davinciLines = devices => {
        const lines = [];
        for (let i = 0; i < devices; i += 4) {
          const group = [];
          for (let k = i; k < Math.min(i + 4, devices); k++) {
            group.push(`--device=/dev/davinci${k}`);
          }
          lines.push("  " + group.join(" "));
        }
        return lines;
      };
      const gpuAccessLines = vendorOf(sel.hw) === "amd" ? ["docker run", "  --device=/dev/kfd --device=/dev/dri", "  --group-add video", "  --cap-add=SYS_PTRACE --security-opt seccomp=unconfined", "  --shm-size 32g"] : vendorOf(sel.hw) === "npu" ? ["docker run --privileged --shm-size=16g", ...davinciLines((catalogEntryOf(sel.hw) || ({})).npuDevices || 16), "  --device=/dev/davinci_manager", "  --device=/dev/hisi_hdc", "  -v /usr/local/sbin:/usr/local/sbin", "  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver", "  -v /usr/local/Ascend/firmware:/usr/local/Ascend/firmware", "  -v /etc/ascend_install.info:/etc/ascend_install.info", "  -v /var/queue_schedule:/var/queue_schedule", "  -v ~/.cache/:/root/.cache/"] : ["docker run --gpus all", "  --shm-size 32g"];
      const dockerLines = [...gpuAccessLines, hostNetwork ? "  --network host" : `  -p ${servePort}:${servePort}`, ...multinode ? fabricFlagsOf(sel.hw).map(f => "  " + f) : [], ...vendorOf(sel.hw) === "npu" ? [] : ["  -v ~/.cache/huggingface:/root/.cache/huggingface"], ...(config.dockerMounts || []).map(mount => `  -v ${mount}`), ...config.placeholders && config.placeholders.HF_TOKEN ? [`  --env "HF_TOKEN={{HF_TOKEN}}"`] : [], ...cellEnv.map(e => `  --env ${e}`), "  --ipc=host", `  ${image}`, `  ${dockerRunCommand}`, ...flags.map(f => "    " + f)];
      cmd = dockerLines.join(" \\\n");
    } else {
      const flagBlock = flags.map(f => "  " + f).join(" \\\n");
      const envBlock = cellEnv.length ? cellEnv.join(" \\\n") + " \\\n" : "";
      cmd = `${envBlock}sglang serve \\\n${flagBlock}`;
    }
    const hintLines = [...overlayHints(sel), ...multinode && config.multiNodeHints && config.multiNodeHints[sel.hw] ? config.multiNodeHints[sel.hw] : []];
    if (hintLines.length) {
      const hint = hintLines.map(line => line.length ? "# " + line : "#").join("\n");
      cmd = `${hint}\n${cmd}`;
    }
    cmd = interpolate(cmd, envValues, modelName);
    if (multinode) {
      const header = `# Multi-node (${nnodes} nodes). Run the same command on every node with:\n` + `#   <node-rank> = 0 on the head node, 1..${nnodes - 1} on the others\n` + `#   <node0-ip>  = IP of the head node (reachable from all others)`;
      cmd = `${header}\n${cmd}`;
    }
    return cmd;
  };
  const ACCURACY_LABELS = config.accuracyLabels || [];
  const renderBenchmarkCard = entry => {
    const pct = entry && entry.latencyPercentile || config.latencyPercentile || "P50";
    const SPEED_LABELS = [["ttft_ms", `TTFT (${pct})`, "ms"], ["tpot_ms", `TPOT (${pct})`, "ms"], ["tokens_per_sec_per_gpu", "throughput per gpu", "tok/s"], ["interactivity", "interactivity", "tokens/s/user", m => m.tpot_ms != null && m.tpot_ms !== 0 ? Math.round(1000 / m.tpot_ms * 10) / 10 : null]];
    const WORKLOAD_KEYS = ["dataset", "isl", "osl", "max_concurrency"];
    const fmt = (val, unit) => {
      if (val === null || val === undefined) return null;
      return `${val}${unit ? " " + unit : ""}`;
    };
    const formatWorkloadParts = (workload, keys) => {
      if (!workload) return "";
      const parts = [];
      if (keys.has("dataset") && workload.dataset) parts.push(workload.dataset);
      if (keys.has("isl") || keys.has("osl")) {
        if (workload.isl != null || workload.osl != null) {
          parts.push(`in/out=${workload.isl != null ? workload.isl : "?"}/${workload.osl != null ? workload.osl : "?"}`);
        }
      }
      if (keys.has("max_concurrency") && workload.max_concurrency != null) {
        parts.push(`max-concurrency=${workload.max_concurrency}`);
      }
      return parts.join(", ");
    };
    const ALWAYS_PER_COLUMN = new Set(["max_concurrency"]);
    const ATOMIC_WORKLOAD_GROUPS = [["isl", "osl"]];
    const partitionWorkload = measurements => {
      const shared = new Set();
      const differing = new Set();
      for (const k of WORKLOAD_KEYS) {
        const seen = new Set();
        let anyPresent = false;
        for (const m of measurements) {
          const v = m && m.workload ? m.workload[k] : undefined;
          if (v != null) anyPresent = true;
          seen.add(v);
        }
        if (!anyPresent) continue;
        if (ALWAYS_PER_COLUMN.has(k) || seen.size > 1) differing.add(k); else shared.add(k);
      }
      for (const group of ATOMIC_WORKLOAD_GROUPS) {
        if (!group.some(k => differing.has(k))) continue;
        for (const k of group) if (shared.delete(k)) differing.add(k);
      }
      return {
        shared,
        differing
      };
    };
    const renderBenchTable = ({title, sharedText, colHeaders, rows, colCount, legend}) => {
      if (rows.length === 0) return null;
      const showColHeaders = colHeaders.length > 0 && colHeaders.some(h => h !== "");
      return <div style={s.benchBlock}>
          <div style={s.benchBlockTitle}>{title}</div>
          {sharedText && <div style={s.benchWorkload}>{sharedText}</div>}
          {}
          <div className="sg-bench-scroll" style={s.benchTableScroll} {...colCount > 1 ? {
        tabIndex: 0,
        role: "group",
        "aria-label": sharedText ? `${title} — ${sharedText}` : title
      } : {}}>
            <div style={{
        ...s.benchTable,
        gridTemplateColumns: `max-content repeat(${colCount}, minmax(max-content, 1fr))`
      }}>
              {showColHeaders && <div key="corner" style={s.benchTableCornerHead}></div>}
              {showColHeaders && colHeaders.map((h, i) => <div key={`hdr-${i}`} style={s.benchTableHead}>{h}</div>)}
              {showColHeaders && <div key="sep" style={s.benchTableSeparator}></div>}
              {rows.map(r => [<div key={`lbl-${r.label}`} style={s.benchTableLabel}>{r.label}</div>, ...r.values.map((v, i) => <div key={`val-${r.label}-${i}`} style={v === null ? {
        ...s.benchTableValue,
        ...s.benchTableValueMissing
      } : s.benchTableValue}>
                    {v !== null ? v : "—"}
                  </div>)])}
            </div>
          </div>
          {legend && <div style={s.benchLegend}>
              {(Array.isArray(legend) ? legend : [legend]).map((line, i) => <div key={`legend-${i}`}>{line}</div>)}
            </div>}
        </div>;
    };
    const buildSpeedTable = measurements => {
      if (measurements.length === 0) return null;
      const {shared, differing} = partitionWorkload(measurements);
      const sharedText = formatWorkloadParts(measurements[0] && measurements[0].workload, shared);
      const colHeaders = measurements.map(m => formatWorkloadParts(m && m.workload, differing));
      const rows = SPEED_LABELS.map(tup => {
        const [key, label, unit, compute] = tup;
        const values = measurements.map(m => {
          const raw = compute ? compute(m) : m[key];
          return fmt(raw, unit);
        });
        return {
          label,
          values
        };
      });
      return {
        title: "Speed",
        sharedText,
        colHeaders,
        rows,
        colCount: measurements.length,
        legend: [`throughput per gpu = (input+output tokens)/elapsed/GPU`, `interactivity = 1000/TPOT(ms) (tokens/s/user)`]
      };
    };
    const buildAccuracyTable = accuracy => {
      if (!accuracy) return null;
      const rows = ACCURACY_LABELS.map(([key, label, unit]) => {
        const v = fmt(accuracy[key], unit);
        if (v === null) return null;
        return {
          label,
          values: [v]
        };
      }).filter(r => r !== null);
      if (rows.length === 0) return null;
      return {
        title: "Accuracy",
        sharedText: null,
        colHeaders: [],
        rows,
        colCount: 1
      };
    };
    const accuracy = effectiveAccuracy(entry, sel);
    const isEmpty = benchmarkIsEmpty(entry, accuracy);
    const measurements = !isEmpty ? normalizeSpeed(entry && entry.speed) : [];
    const accuracyTable = !isEmpty ? buildAccuracyTable(accuracy) : null;
    const speedTable = !isEmpty ? buildSpeedTable(measurements) : null;
    const hasBenchCmds = !isEmpty && buildBenchCommands(entry, sel) !== null;
    return <div style={s.benchCard}>
        <div style={s.benchHeader}>
          <div style={s.benchTitle}>Benchmark</div>
          <div style={s.benchHeaderRight}>
            {!isEmpty && entry && entry.sglang_version && <div style={s.benchVersion}>measured on sglang <code>{entry.sglang_version}</code></div>}
            {hasBenchCmds && <button style={s.iconButton} onClick={() => setModal("bench")}>⚡ Reproduce</button>}
          </div>
        </div>
        {isEmpty ? <div style={s.benchEmpty}>
            Benchmark data pending for this combination — submit yours via the Playground's Submit ↗ button.
          </div> : <>
            {accuracyTable && renderBenchTable(accuracyTable)}
            {speedTable && renderBenchTable(speedTable)}
            {entry && entry.notes && <div style={s.benchNotes}>{entry.notes}</div>}
          </>}
      </div>;
  };
  const buildBenchCommands = (entry, sel) => {
    const bc = config.benchmarkCommands;
    if (!bc) return null;
    const acc = effectiveAccuracy(entry, sel);
    const accuracy = [];
    if (bc.accuracy) {
      for (const [key, label] of ACCURACY_LABELS) {
        if (acc[key] == null) continue;
        const tmpl = bc.accuracy[key];
        const resolved = typeof tmpl === "string" ? tmpl : tmpl && tmpl[sel.variant] || null;
        if (resolved) accuracy.push({
          key,
          label,
          template: resolved
        });
      }
    }
    let speed = null;
    if (bc.speed && entry) {
      const ms = normalizeSpeed(entry.speed).filter(m => m && m.workload && m.workload.max_concurrency != null);
      const concurrencies = [...new Set(ms.map(m => m.workload.max_concurrency))].sort((a, b) => a - b);
      if (concurrencies.length) {
        speed = {
          template: bc.speed,
          concurrencies,
          workload: ms[0].workload,
          numPromptsOf: c => {
            const m = ms.find(x => x.workload.max_concurrency === c);
            if (m && m.workload.num_prompts != null) return m.workload.num_prompts;
            const tbl = bc.numPromptsByConc;
            if (tbl && tbl[c] != null) return tbl[c];
            return Math.max(c * 2, 200);
          }
        };
      }
    }
    if (accuracy.length === 0 && !speed) return null;
    return {
      accuracy,
      speed
    };
  };
  const buildHardwareGroups = () => {
    const supported = new Set(config.supportedHardware);
    const catalog = {};
    for (const [vendor, list] of Object.entries(HARDWARE_CATALOG)) catalog[vendor] = [...list];
    for (const hw of config.hardware || []) {
      const vendor = hw.vendor || "nvidia";
      const list = catalog[vendor] || (catalog[vendor] = []);
      const entry = {
        id: hw.id,
        label: hw.label,
        vram: hw.vram
      };
      const i = list.findIndex(x => x.id === hw.id);
      if (i >= 0) list[i] = entry; else list.push(entry);
    }
    const groups = [];
    for (const [vendor, list] of Object.entries(catalog)) {
      const items = list.filter(hw => supported.has(hw.id)).map(hw => ({
        id: hw.id,
        label: hw.label,
        subtitle: hw.vram
      }));
      if (items.length) groups.push({
        label: vendor.toUpperCase(),
        items
      });
    }
    if (config.groupHardware === false) {
      return [{
        label: null,
        items: groups.flatMap(group => group.items)
      }];
    }
    return groups;
  };
  const initialSelectionFromCells = () => {
    const first = (config.cells || [])[0];
    const sel = Object.fromEntries(DIMENSIONS.map(d => [d, first ? first.match[d] : ""]));
    for (const spec of overlayDimSpecs) {
      const opts = spec.options || [];
      sel[spec.id] = (spec.default ?? (opts[0] && opts[0].id)) ?? "";
    }
    if (!commandBuilder) return sel;
    return {
      ...sel,
      hw: commandBuilder.defaultSelection?.hw || config.supportedHardware?.[0] || "",
      ...commandBuilder.defaultSelection || ({})
    };
  };
  const normalizeBuilderSelection = parsed => {
    const out = {
      ...initialSelectionFromCells(),
      ...parsed
    };
    if (!(config.supportedHardware || []).includes(out.hw)) {
      out.hw = commandBuilder.defaultSelection?.hw || config.supportedHardware?.[0] || "";
    }
    for (const spec of overlayDimSpecs) {
      if (spec.kind === "number") {
        const value = Number.parseInt(out[spec.id], 10);
        out[spec.id] = Math.min(spec.max, Math.max(spec.min, Number.isFinite(value) ? value : Number(spec.default ?? spec.min)));
        continue;
      }
      const options = spec.options || [];
      if (!options.some(option => option.id === out[spec.id])) {
        out[spec.id] = (spec.default ?? options[0]?.id) ?? "";
      }
    }
    for (const [key, bounds] of Object.entries(commandBuilder.resource?.limits || ({}))) {
      const fallback = Number((commandBuilder.defaultSelection?.[key] ?? bounds.min) ?? 1);
      const value = Number.parseInt(out[key], 10);
      out[key] = Math.min(bounds.max, Math.max(bounds.min, Number.isFinite(value) ? value : fallback));
    }
    for (const key of ["tp_size", "ulysses_degree", "ring_degree"]) {
      const value = Number.parseInt(out[key], 10);
      out[key] = Number.isFinite(value) && value > 0 ? value : 1;
    }
    out.topology_mode = out.topology_mode === "manual" ? "manual" : "auto";
    return out;
  };
  const placeholderDefaults = schema => {
    const out = {};
    for (const [k, v] of Object.entries(schema || ({}))) out[k] = v.default ?? "";
    return out;
  };
  const [isDark, setIsDark] = useState(false);
  useEffect(() => {
    const check = () => {
      const html = document.documentElement;
      setIsDark(html.classList.contains("dark") || html.getAttribute("data-theme") === "dark" || html.style.colorScheme === "dark");
    };
    check();
    const observer = new MutationObserver(check);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class", "data-theme", "style"]
    });
    return () => observer.disconnect();
  }, []);
  const STORAGE_KEY = "sglang-deploy-env";
  const [env, setEnv] = useState(() => placeholderDefaults(config.placeholders));
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        setEnv({
          ...placeholderDefaults(config.placeholders),
          ...parsed
        });
      }
    } catch {}
  }, []);
  const saveEnv = next => {
    setEnv(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    } catch {}
  };
  const [sel, setSel] = useState(() => initialSelectionFromCells());
  const [selectionHydrated, setSelectionHydrated] = useState(false);
  const INTERNAL_HASH_STATE_KEY = "__sglangDeployInternalHash";
  const DEPLOYMENT_COMPONENT_ID = "deployment-configurator";
  useEffect(() => {
    const hydrate = () => {
      const raw = window.location.hash.replace(/^#/, "");
      if (!raw) return;
      const params = new URLSearchParams(raw);
      const initial = initialSelectionFromCells();
      const parsed = {
        ...initial
      };
      let touched = false;
      params.forEach((value, key) => {
        if ((key in parsed)) {
          parsed[key] = value;
          touched = true;
        }
      });
      if (!touched) return;
      setSel(commandBuilder ? normalizeBuilderSelection(parsed) : validateSelection(config.cells, parsed));
      const historyState = window.history.state;
      const isInternalHash = historyState && typeof historyState === "object" && historyState[INTERNAL_HASH_STATE_KEY] === `#${raw}`;
      if (isInternalHash) return;
      const el = document.getElementById(DEPLOYMENT_COMPONENT_ID);
      if (el) el.scrollIntoView({
        behavior: "smooth",
        block: "start"
      });
    };
    hydrate();
    setSelectionHydrated(true);
    window.addEventListener("hashchange", hydrate);
    return () => window.removeEventListener("hashchange", hydrate);
  }, []);
  useEffect(() => {
    if (!selectionHydrated) return;
    const target = "#" + new URLSearchParams(sel).toString();
    if (window.location.hash !== target) {
      const historyState = window.history.state && typeof window.history.state === "object" ? window.history.state : {};
      window.history.replaceState({
        ...historyState,
        [INTERNAL_HASH_STATE_KEY]: target
      }, "", target);
    }
    window.dispatchEvent(new CustomEvent("sglang-deploy-sel", {
      detail: sel
    }));
  }, [sel, selectionHydrated]);
  const [modal, setModal] = useState(null);
  useEffect(() => {
    if (modal === null) return;
    const onKey = e => {
      if (e.key === "Escape") setModal(null);
    };
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [modal]);
  const [copied, setCopied] = useState(false);
  const [curlCopied, setCurlCopied] = useState(false);
  const [envDraft, setEnvDraft] = useState(env);
  const [benchConc, setBenchConc] = useState(null);
  const [benchAcc, setBenchAcc] = useState(null);
  const [benchCopied, setBenchCopied] = useState(null);
  const configuredRunModes = typeof config.runModes === "function" ? config.runModes(sel) : config.runModes;
  const runModes = configuredRunModes || ["python", "docker"];
  const [runMode, setRunMode] = useState(runModes[0]);
  const [builderScope, setBuilderScope] = useState("base");
  const [builderServerSetting, setBuilderServerSetting] = useState(null);
  const [builderAdvanced, setBuilderAdvanced] = useState(false);
  const [serveExpanded, setServeExpanded] = useState(false);
  const [requestExpanded, setRequestExpanded] = useState(false);
  const [builderHeadAddress, setBuilderHeadAddress] = useState("<head-node-ip>");
  const [builderNodeRank, setBuilderNodeRank] = useState(0);
  const [blockedNote, setBlockedNote] = useState(null);
  const flashBlockedNote = (dim, reason) => {
    const note = {
      dim,
      reason
    };
    setBlockedNote(note);
    setTimeout(() => setBlockedNote(cur => cur === note ? null : cur), 4000);
  };
  useEffect(() => {
    if (builderNodeRank >= Number(sel.nodes || 1)) setBuilderNodeRank(0);
  }, [sel.nodes, builderNodeRank]);
  const hasRunMode = runModes.includes(runMode);
  const fallbackRunMode = runModes[0];
  const activeRunMode = hasRunMode ? runMode : fallbackRunMode;
  useEffect(() => {
    if (!hasRunMode) setRunMode(fallbackRunMode);
  }, [hasRunMode, fallbackRunMode]);
  useEffect(() => {
    if (modal === "env") setEnvDraft(env);
  }, [modal, env]);
  const [mambaRatio, setMambaRatio] = useState(null);
  useEffect(() => {
    const onRatio = e => setMambaRatio(e.detail && (e.detail.baseRatio || e.detail.ratio) || null);
    window.addEventListener("sglang-k3-mamba-ratio", onRatio);
    return () => window.removeEventListener("sglang-k3-mamba-ratio", onRatio);
  }, []);
  const s = makeStyles(isDark);
  const cell = commandBuilder ? commandBuilder.resolveDeployment(sel) : findCell(config.cells, sel);
  const builderMeta = cell && cell.builder || ({});
  const verifyStatus = cellVerifyStatus(cell, sel);
  const cellWithRatio = (() => {
    if (!cell || !mambaRatio) return cell;
    if (cell.flags.some(f => f.startsWith("--mamba-full-memory-ratio") || f.startsWith("--max-mamba-cache-size"))) return cell;
    const flags = [...cell.flags];
    const line = `--mamba-full-memory-ratio ${mambaRatio}`;
    const i = flags.findIndex(f => f.startsWith("--host"));
    if (i >= 0) flags.splice(i, 0, line); else flags.push(line);
    return {
      ...cell,
      flags
    };
  })();
  const commandEnv = commandBuilder ? {
    ...env,
    NODE_RANK: String(builderNodeRank),
    NODE0_IP: builderHeadAddress || "<head-node-ip>"
  } : env;
  const command = renderCommand(cellWithRatio, sel, commandEnv, activeRunMode);
  const effFlags = cell ? overlayCompose(cell.flags, sel) : [];
  const specAlgoFlag = effFlags.find(f => f.split(/[\s=]/)[0] === "--speculative-algorithm");
  const specMrrFlag = effFlags.find(f => f.split(/[\s=]/)[0] === "--max-running-requests");
  const mtpHint = !!specAlgoFlag && !specMrrFlag;
  const specPinnedHint = !!specAlgoFlag && !!specMrrFlag;
  const specMrrValue = specMrrFlag ? specMrrFlag.split(/[\s=]/).filter(Boolean)[1] || "" : "";
  const SPEC_ALGO_LABEL = {
    EAGLE: "MTP",
    EAGLE3: "MTP",
    FROZEN_KV_MTP: "MTP",
    DSPARK: "DSpark",
    DFLASH: "DFlash",
    NGRAM: "N-gram",
    STANDALONE: "standalone draft"
  };
  const specAlgoName = (() => {
    if (!specAlgoFlag) return "MTP";
    const v = specAlgoFlag.split(/[\s=]/).filter(Boolean)[1] || "";
    return SPEC_ALGO_LABEL[v.toUpperCase()] || v || "MTP";
  })();
  const renderWarn = text => {
    const out = [];
    const re = /\[([^\]]+)\]\(#([^)]+)\)/g;
    let last = 0;
    for (let m; m = re.exec(text); last = m.index + m[0].length) {
      if (m.index > last) out.push(text.slice(last, m.index));
      const anchor = m[2];
      out.push(<button key={m.index} type="button" onClick={() => {
        const el = document.getElementById(anchor);
        if (el) el.scrollIntoView({
          behavior: "smooth",
          block: "start"
        });
      }} style={{
        background: "transparent",
        border: "none",
        padding: 0,
        color: isDark ? "#FDBA74" : "#C2410C",
        cursor: "pointer",
        font: "inherit",
        fontWeight: 600,
        textDecoration: "underline",
        textUnderlineOffset: "2px"
      }}>
          {m[1]}
        </button>);
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  };
  const modelName = resolveModelName(sel);
  const curlTemplate = typeof config.curl === "function" ? config.curl(sel, cell) : config.curl;
  const curlText = interpolate(curlTemplate || "", env, modelName);
  const hwGroups = buildHardwareGroups();
  const benchEntry = benchmarks ? findBenchmark(benchmarks, sel) : null;
  const isOverlayDim = dim => overlayDimSpecs.some(d => d.id === dim);
  const findOption = (dim, value) => {
    const spec = [...matchDimSpecs, ...overlayDimSpecs].find(d => d.id === dim);
    return spec && (spec.options || []).find(o => o.id === value);
  };
  const isEnabled = (dim, value) => {
    const opt = findOption(dim, value);
    if (opt && optionDisabled(opt, sel)) return false;
    if (commandBuilder && dim === "hw") return true;
    return isOverlayDim(dim) || isOptionAvailable(config.cells || [], sel, dim, value);
  };
  const reseatHiddenPicks = next => {
    let out = next;
    for (const spec of [...matchDimSpecs, ...overlayDimSpecs]) {
      const opts = visibleOptions(spec, out).filter(o => !optionDisabled(o, out));
      if (!opts.length) continue;
      if (!opts.some(o => o.id === out[spec.id])) {
        out = {
          ...out,
          [spec.id]: opts[0].id
        };
      }
    }
    return out;
  };
  const recommendedBuilderRecipe = hw => {
    const recipes = commandBuilder.resource?.verifiedRecipes || [];
    return recipes.find(entry => entry.hw === hw && entry.default) || recipes.find(entry => entry.hw === hw);
  };
  const handleSelect = (dim, value) => {
    if (commandBuilder) {
      setSel(prev => {
        let next = {
          ...prev,
          [dim]: value
        };
        if (dim === "hw") {
          const currentRecipe = recommendedBuilderRecipe(prev.hw);
          const nextRecipe = recommendedBuilderRecipe(value);
          const resourcesFollowPlatformDefault = !!currentRecipe && Number(prev.nodes) === Number(currentRecipe.nodes) && Number(prev.gpus_per_node) === Number(currentRecipe.gpus_per_node);
          next = {
            ...next,
            nodes: resourcesFollowPlatformDefault ? nextRecipe?.nodes ?? next.nodes : next.nodes,
            gpus_per_node: resourcesFollowPlatformDefault ? nextRecipe?.gpus_per_node ?? next.gpus_per_node : next.gpus_per_node,
            topology_mode: "auto",
            tp_size: resourcesFollowPlatformDefault ? nextRecipe?.tp_size ?? 1 : next.tp_size,
            ulysses_degree: resourcesFollowPlatformDefault ? nextRecipe?.ulysses_degree ?? 1 : next.ulysses_degree,
            ring_degree: resourcesFollowPlatformDefault ? nextRecipe?.ring_degree ?? 1 : next.ring_degree,
            placement: resourcesFollowPlatformDefault ? nextRecipe?.placement || "auto" : next.placement,
            encoder: resourcesFollowPlatformDefault ? nextRecipe?.encoder || "auto" : next.encoder
          };
        }
        return reseatHiddenPicks(normalizeBuilderSelection(next));
      });
      return;
    }
    setSel(prev => reseatHiddenPicks(isOverlayDim(dim) ? {
      ...prev,
      [dim]: value
    } : snapToValidCell(config.cells, prev, dim, value)));
  };
  const commitBuilderNumber = (event, currentValue, bounds, commit) => {
    const parsed = Number(event.currentTarget.value);
    if (!Number.isInteger(parsed)) {
      event.currentTarget.value = String(currentValue);
      return;
    }
    const value = Math.min(bounds.max, Math.max(bounds.min, parsed));
    event.currentTarget.value = String(value);
    commit(value);
  };
  const renderBuilderNumberInput = ({identity, value, min, max, label, onCommit}) => <input key={identity} type="number" inputMode="numeric" min={min} max={max} step="1" defaultValue={value} aria-label={label} onFocus={event => event.currentTarget.select()} onBlur={event => commitBuilderNumber(event, value, {
    min,
    max
  }, onCommit)} onKeyDown={event => {
    if (event.key === "Enter") event.currentTarget.blur();
  }} />;
  const updateBuilderResource = (key, delta) => {
    if (!commandBuilder) return;
    const bounds = commandBuilder.resource?.limits?.[key] || ({
      min: 1,
      max: 8
    });
    setSel(prev => {
      const value = Math.min(bounds.max, Math.max(bounds.min, Number(prev[key]) + delta));
      return normalizeBuilderSelection({
        ...prev,
        [key]: value,
        topology_mode: "auto"
      });
    });
  };
  const setBuilderResource = (key, rawValue) => {
    if (!commandBuilder) return;
    const value = Number.parseInt(rawValue, 10);
    if (!Number.isFinite(value)) return;
    const bounds = commandBuilder.resource?.limits?.[key] || ({
      min: 1,
      max: 8
    });
    setSel(prev => normalizeBuilderSelection({
      ...prev,
      [key]: Math.min(bounds.max, Math.max(bounds.min, value)),
      topology_mode: "auto"
    }));
  };
  const editBuilderTopology = (key, value) => {
    if (!commandBuilder) return;
    setSel(prev => normalizeBuilderSelection({
      ...prev,
      topology_mode: "manual",
      [key]: Number.parseInt(value, 10) || 1
    }));
  };
  const handleCopy = () => {
    navigator.clipboard.writeText(command);
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  };
  const copyCurl = () => {
    navigator.clipboard.writeText(curlText);
    setCurlCopied(true);
    setTimeout(() => setCurlCopied(false), 1200);
  };
  const copyBench = (key, text) => {
    navigator.clipboard.writeText(text);
    setBenchCopied(key);
    setTimeout(() => setBenchCopied(null), 1200);
  };
  const placeholderGroups = (() => {
    const out = {
      command: [],
      curl: []
    };
    for (const [key, meta] of Object.entries(config.placeholders || ({}))) {
      (out[meta.target] || (out[meta.target] = [])).push({
        key,
        ...meta
      });
    }
    return out;
  })();
  const renderButton = (item, dim, selectedId) => {
    const checked = selectedId === item.id;
    const disabled = !isEnabled(dim, item.id);
    return <label key={item.id} className="sg-command-visualizer-choice" role="radio" aria-checked={checked} aria-disabled={disabled} tabIndex={disabled ? -1 : 0} style={{
      ...s.labelBase,
      ...checked ? s.checked : {},
      ...disabled ? s.disabled : {}
    }} title={disabled ? (typeof item.disableReason === "function" ? item.disableReason(sel) : item.disableReason) || "Not supported for current selection" : ""} onClick={e => {
      if (disabled) {
        e.preventDefault();
        return;
      }
      handleSelect(dim, item.id);
    }} onKeyDown={e => {
      if (disabled || e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      handleSelect(dim, item.id);
    }}>
        <input type="radio" checked={checked} disabled={disabled} readOnly style={{
      display: "none"
    }} />
        <span>{item.label}</span>
        {item.subtitle && <small style={{
      ...s.subtitle,
      color: checked ? "rgba(255,255,255,0.85)" : "inherit"
    }}>
            {item.subtitle}
          </small>}
      </label>;
  };
  const renderFlatSection = (title, options, dim, selectedId) => <div style={s.card}>
      <div style={s.title}>{title}</div>
      <div style={s.itemsGrid(options.length)}>
        {options.map(item => renderButton(item, dim, selectedId))}
      </div>
    </div>;
  const maxHwCols = Math.max(...hwGroups.map(x => x.items.length));
  if (commandBuilder) {
    const scopeLabel = {
      base: "Setup",
      serve: "Server",
      request: "Request"
    };
    const scopedDims = scope => overlayDimSpecs.filter(dim => {
      if ((dim.scope || "base") !== scope) return false;
      if (dim.kind === "number") {
        return typeof dim.showWhen !== "function" || dim.showWhen(sel);
      }
      return rowVisible(dim, sel);
    });
    const baseDims = scopedDims("base");
    const serveDims = scopedDims("serve");
    const requestDims = scopedDims("request");
    const errors = builderMeta.errors || [];
    const warnings = builderMeta.warnings || [];
    const invalid = errors.length > 0;
    const totalGpus = Number(sel.nodes) * Number(sel.gpus_per_node);
    const topology = builderMeta.topology || ({});
    const verification = builderMeta.verification || ({});
    const scopeIsVerified = scope => scopedDims(scope).every(dim => {
      const option = (dim.options || []).find(entry => entry.id === sel[dim.id]);
      if (option && optionSoft(option, sel)) return false;
      const predicate = option?.verifiedWhen ?? dim.verifiedWhen;
      return typeof predicate === "function" ? !!predicate(sel) : predicate !== false;
    });
    const serveStatus = invalid ? "error" : scopeIsVerified("serve") ? verification.serve || verifyStatus : "unverified";
    const requestStatus = invalid ? "error" : scopeIsVerified("request") ? verification.request || verifyStatus : "unverified";
    const statusText = status => ({
      verified: "Verified",
      unverified: "Unverified",
      "in-progress": "Verification in progress",
      error: "Invalid configuration"
    })[status] || "Unverified";
    const activeServerSetting = serveDims.find(dim => dim.id === builderServerSetting) || serveDims[0];
    const selectedOption = dim => (dim.options || []).find(option => option.id === sel[dim.id]);
    const effectiveSetting = dim => builderMeta.resolvedSettings?.[dim.id] || selectedOption(dim)?.label || sel[dim.id] || "—";
    const recommendedRecipe = recommendedBuilderRecipe(sel.hw);
    const recommendedInUse = !!recommendedRecipe && Number(sel.nodes) === recommendedRecipe.nodes && Number(sel.gpus_per_node) === recommendedRecipe.gpus_per_node && sel.topology_mode === "auto" && serveDims.every(dim => {
      const expected = recommendedRecipe[dim.id] ?? dim.default;
      if (dim.id === "attention") return sel.attention === "platform";
      return expected === undefined || sel[dim.id] === expected || ["placement", "encoder"].includes(dim.id) && sel[dim.id] === "auto";
    });
    const restoreRecommendedRecipe = () => {
      if (!recommendedRecipe) return;
      setSel(prev => reseatHiddenPicks(normalizeBuilderSelection({
        ...prev,
        ...Object.fromEntries(serveDims.map(dim => [dim.id, (recommendedRecipe[dim.id] ?? dim.default) ?? dim.options?.[0]?.id])),
        nodes: recommendedRecipe.nodes,
        gpus_per_node: recommendedRecipe.gpus_per_node,
        topology_mode: "auto",
        tp_size: recommendedRecipe.tp_size,
        ulysses_degree: recommendedRecipe.ulysses_degree,
        ring_degree: recommendedRecipe.ring_degree,
        placement: recommendedRecipe.placement || "auto",
        attention: "platform",
        precision: "native",
        encoder: recommendedRecipe.encoder || "auto",
        execution: recommendedRecipe.execution || "eager"
      })));
    };
    const renderBuilderChoice = (item, dim) => {
      const checked = sel[dim.id] === item.id;
      const disabled = !isEnabled(dim.id, item.id);
      const soft = !disabled && optionSoft(item, sel);
      const reason = disabled ? item.disableReason || "Not available for this configuration" : soft ? item.softReason || "Runs, but this combination is not a verified recipe yet." : "";
      return <button key={item.id} type="button" className="sgd-builder-choice" data-selected={checked ? "true" : "false"} data-blocked={disabled ? "true" : undefined} data-soft={soft ? "true" : undefined} aria-disabled={disabled} aria-pressed={checked} title={reason} onClick={() => {
        if (disabled) {
          flashBlockedNote(dim.id, reason);
          return;
        }
        handleSelect(dim.id, item.id);
      }}>
          <span className="sgd-builder-choice-dot" aria-hidden="true" />
          <span>{item.label}</span>
          {item.subtitle && <small>{item.subtitle}</small>}
        </button>;
    };
    const renderBuilderDimension = dim => <section className="sgd-builder-section" key={dim.id}>
        <div className="sgd-builder-section-heading">
          <span>{dim.title}</span>
          {dim.description && <small>{dim.description}</small>}
        </div>
        <div className="sgd-builder-choice-grid" data-density={(dim.options || []).length > 5 ? "compact" : "normal"}>
          {visibleOptions(dim, sel).map(option => renderBuilderChoice(option, dim))}
        </div>
        {blockedNote && blockedNote.dim === dim.id && <p className="sgd-builder-blocked-note" role="status">{blockedNote.reason}</p>}
      </section>;
    const renderStepper = (key, label, detail) => {
      const bounds = commandBuilder.resource?.limits?.[key] || ({
        min: 1,
        max: 8
      });
      return <div className="sgd-builder-stepper-field">
          <div>
            <span>{label}</span>
            {detail && <small>{detail}</small>}
          </div>
          <div className="sgd-builder-stepper" aria-label={label}>
            <button type="button" aria-label={`Decrease ${label}`} disabled={Number(sel[key]) <= bounds.min} onClick={() => updateBuilderResource(key, -1)}>−</button>
            {renderBuilderNumberInput({
        identity: `${key}-${sel[key]}`,
        value: sel[key],
        min: bounds.min,
        max: bounds.max,
        label,
        onCommit: value => setBuilderResource(key, value)
      })}
            <button type="button" aria-label={`Increase ${label}`} disabled={Number(sel[key]) >= bounds.max} onClick={() => updateBuilderResource(key, 1)}>+</button>
          </div>
        </div>;
    };
    const renderBaseScope = () => <div className="sgd-builder-scope-panel" data-scope="base">
        {recommendedRecipe && <section className="sgd-builder-recipe">
            <div>
              {}
              <span>{recommendedRecipe.unverified ? "Derived recipe" : "Verified recipe"} · {sel.hw.toUpperCase()}</span>
              <strong>
                {[`${recommendedRecipe.nodes * recommendedRecipe.gpus_per_node} GPUs`, recommendedRecipe.tp_size > 1 && `TP ${recommendedRecipe.tp_size}`, `Ulysses ${recommendedRecipe.ulysses_degree}`, recommendedRecipe.ring_degree > 1 && `Ring ${recommendedRecipe.ring_degree}`, ({
      resident: "Resident",
      fsdp: "FSDP",
      offload: "Layerwise offload"
    })[recommendedRecipe.placement]].filter(Boolean).join(" · ")}
              </strong>
            </div>
            <div>
              {renderStatus(recommendedRecipe.unverified ? "unverified" : "verified")}
              {recommendedInUse ? <small>In use</small> : <button type="button" className="sgd-builder-text-action" onClick={restoreRecommendedRecipe}>{recommendedRecipe.unverified ? "Use derived recipe" : "Use verified recipe"}</button>}
            </div>
          </section>}
        <section className="sgd-builder-section">
          <div className="sgd-builder-section-heading"><span>Hardware</span></div>
          <div className="sgd-builder-hardware-grid">
            {hwGroups.flatMap(group => group.items).map(item => {
      const selected = sel.hw === item.id;
      return <button key={item.id} type="button" className="sgd-builder-hardware" data-selected={selected ? "true" : "false"} aria-pressed={selected} onClick={() => handleSelect("hw", item.id)}>
                  <span className="sgd-builder-choice-dot" aria-hidden="true" />
                  <strong>{item.label}</strong>
                  <small>{item.subtitle}</small>
                </button>;
    })}
          </div>
        </section>

        {}
        <section className="sgd-builder-section">
          <div className="sgd-builder-section-heading">
            <span>Resources</span>
            <small>{builderMeta.topologySummary || "No valid topology"}</small>
          </div>
          <div className="sgd-builder-resource-grid">
            {renderStepper("nodes", "Nodes")}
            {renderStepper("gpus_per_node", "GPUs / node")}
          </div>
          {Number(sel.nodes) > 1 && <p className="sgd-builder-resource-summary">
              {sel.nodes} nodes × {sel.gpus_per_node} {sel.hw.toUpperCase()} = {totalGpus} GPUs
            </p>}
          <button type="button" className="sgd-builder-text-action sgd-builder-topology-toggle" aria-expanded={builderAdvanced} onClick={() => setBuilderAdvanced(open => !open)}>
            Advanced topology <span aria-hidden="true">{builderAdvanced ? "↗" : "↘"}</span>
          </button>
          {builderAdvanced && <div className="sgd-builder-advanced">
              <p>Auto uses an exact verified recipe when one exists; manual values are allowed when the model constraints remain valid.</p>
              <div className="sgd-builder-topology-inputs">
                {[["tp_size", "Tensor parallel", [1, 2, 4, 8]], ["ulysses_degree", "Ulysses", [1, 2, 4, 8, 16]], ["ring_degree", "Ring", [1, 2, 4, 8]]].map(([key, label, values]) => <label key={key}>
                    <span>{label}</span>
                    <select value={sel.topology_mode === "manual" ? sel[key] : topology[key] || 1} onChange={event => editBuilderTopology(key, event.target.value)}>
                      {values.map(value => <option value={value} key={value}>{value}</option>)}
                    </select>
                  </label>)}
              </div>
              <button type="button" className="sgd-builder-text-action" disabled={sel.topology_mode === "auto"} onClick={() => setSel(prev => normalizeBuilderSelection({
      ...prev,
      topology_mode: "auto"
    }))}>Use automatic topology</button>
            </div>}
          {(errors.length > 0 || warnings.length > 0) && <div className="sgd-builder-messages" data-state={errors.length ? "error" : "warning"}>
              {(errors.length ? errors : warnings).map((message, index) => <p key={index}>{message}</p>)}
            </div>}
        </section>

        {baseDims.map(renderBuilderDimension)}
      </div>;
    const renderSettingEditor = (dim, className = "", direct = false) => {
      if (!dim) return null;
      const options = visibleOptions(dim, sel);
      const currentOption = selectedOption(dim);
      return <section className={["sgd-builder-context", className].filter(Boolean).join(" ")} aria-live={direct ? undefined : "polite"}>
          <div className="sgd-builder-context-heading">
            <div>
              <span>{direct ? dim.title : `${dim.title} options`}</span>
              {dim.description && <p>{dim.description}</p>}
            </div>
            {dim.quality && <small>{dim.quality}</small>}
          </div>
          {dim.kind === "number" ? <div className="sgd-builder-request-stepper">
              <button type="button" aria-label={`Decrease ${dim.title}`} disabled={Number(sel[dim.id]) <= dim.min} onClick={() => setSel(prev => ({
        ...prev,
        [dim.id]: Math.max(dim.min, Number(prev[dim.id]) - 1)
      }))}>−</button>
              {renderBuilderNumberInput({
        identity: `${dim.id}-${sel[dim.id]}`,
        value: sel[dim.id],
        min: dim.min,
        max: dim.max,
        label: dim.title,
        onCommit: value => setSel(prev => ({
          ...prev,
          [dim.id]: value
        }))
      })}
              <button type="button" aria-label={`Increase ${dim.title}`} disabled={Number(sel[dim.id]) >= dim.max} onClick={() => setSel(prev => ({
        ...prev,
        [dim.id]: Math.min(dim.max, Number(prev[dim.id]) + 1)
      }))}>+</button>
              <span>{dim.unit || "outputs"}</span>
            </div> : <div className="sgd-builder-context-options">
              {options.map(option => renderBuilderChoice(option, dim))}
            </div>}
          {blockedNote && blockedNote.dim === dim.id && <p className="sgd-builder-blocked-note" role="status">{blockedNote.reason}</p>}
          {(currentOption?.description || dim.learnMore) && <div className="sgd-builder-context-note">
              {currentOption?.description && <p>{currentOption.description}</p>}
              {}
              {dim.learnMore && <a href={dim.learnMore}>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><line x1="4" y1="6" x2="20" y2="6" /><line x1="4" y1="12" x2="16" y2="12" /><line x1="4" y1="18" x2="11" y2="18" /></svg>
                  Learn more
                </a>}
              {dim.docsHref && <a href={dim.docsHref}>
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></svg>
                  SGLang docs
                </a>}
            </div>}
        </section>;
    };
    const renderServerScope = () => <div className="sgd-builder-scope-panel" data-scope="serve">
        <div className="sgd-builder-setting-layout">
          <div className="sgd-builder-setting-list">
            {serveDims.map(dim => {
      const isActive = dim.id === activeServerSetting?.id;
      const option = selectedOption(dim);
      const recommended = typeof option?.recommendedWhen === "function" ? option.recommendedWhen(sel) : !!option?.recommended;
      return <div className="sgd-builder-setting-item" key={dim.id}>
                  <button type="button" className="sgd-builder-setting-row" data-active={isActive ? "true" : "false"} aria-expanded={isActive} onClick={() => setBuilderServerSetting(dim.id)}>
                    <span>{dim.title}</span>
                    <strong>{effectiveSetting(dim)}</strong>
                    {recommended && <small>Recommended</small>}
                    <span aria-hidden="true">{isActive ? "⌄" : "›"}</span>
                  </button>
                  {isActive && renderSettingEditor(dim, "sgd-builder-context--inline")}
                </div>;
    })}
          </div>
          {renderSettingEditor(activeServerSetting, "sgd-builder-context--rail")}
        </div>
      </div>;
    const renderRequestScope = () => <div className="sgd-builder-scope-panel sgd-builder-request-direct" data-scope="request">
        {requestDims.map(dim => <div className="sgd-builder-request-setting" key={dim.id}>
            {renderSettingEditor(dim, "", true)}
          </div>)}
      </div>;
    const renderScopeControls = () => {
      if (builderScope === "base") return renderBaseScope();
      if (builderScope === "serve") return renderServerScope();
      return renderRequestScope();
    };
    const renderStatus = status => <span className="sgd-builder-status" data-status={status}>
        <span aria-hidden="true" />{statusText(status)}
      </span>;
    const renderOutputCard = type => {
      const serve = type === "serve";
      const text = serve ? command : curlText;
      const canExpand = text.split("\n").length > 9;
      const expanded = serve ? serveExpanded : requestExpanded;
      const setExpanded = serve ? setServeExpanded : setRequestExpanded;
      const status = serve ? serveStatus : requestStatus;
      const emphasized = builderScope === "base" || builderScope === type;
      return <section className="sgd-builder-output" data-output={type} data-emphasized={emphasized ? "true" : "false"}>
          <header>
            <div className="sgd-builder-output-index">{serve ? "1" : "2"}</div>
            <div className="sgd-builder-output-title">
              <strong>{serve ? "Serve" : "Request"}</strong>
              <span>
                {serve ? `${sel.hw.toUpperCase()} · ${activeRunMode === "docker" ? "Docker" : "Python"}` : "cURL"}
              </span>
            </div>
            {renderStatus(status)}
          </header>
          {serve && runModes.length > 1 && <div className="sgd-builder-output-tabs" role="tablist" aria-label="Serve command format">
              {runModes.map(mode => <button type="button" role="tab" aria-selected={activeRunMode === mode} data-selected={activeRunMode === mode ? "true" : "false"} key={mode} onClick={() => setRunMode(mode)}>{mode === "docker" ? "Docker" : "Python"}</button>)}
            </div>}
          {serve && Number(sel.nodes) > 1 && <div className="sgd-builder-node-fields">
              <label>
                <span>Head address</span>
                <input value={builderHeadAddress} onChange={event => setBuilderHeadAddress(event.target.value)} />
              </label>
              <label>
                <span>Node rank</span>
                {renderBuilderNumberInput({
        identity: `node-rank-${builderNodeRank}-${sel.nodes}`,
        value: builderNodeRank,
        min: 0,
        max: Number(sel.nodes) - 1,
        label: "Node rank",
        onCommit: setBuilderNodeRank
      })}
              </label>
            </div>}
          <div className="sgd-builder-code">
            <pre className={expanded ? "is-expanded" : ""}><code>{text}</code></pre>
            <button type="button" className="sgd-builder-copy" disabled={invalid} aria-label={(serve ? copied : curlCopied) ? "Copied" : "Copy command"} data-copied={(serve ? copied : curlCopied) ? "true" : undefined} onClick={serve ? handleCopy : copyCurl}>
              <svg className="sgd-builder-copy-glyph" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2.5" /><path d="M15 5v-.25A2.75 2.75 0 0 0 12.25 2h-7.5A2.75 2.75 0 0 0 2 4.75v7.5A2.75 2.75 0 0 0 4.75 15H5" /></svg>
              <svg className="sgd-builder-copy-check" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
            </button>
          </div>
          {invalid && <div className="sgd-builder-output-error">{errors[0]}</div>}
          <footer>
            {canExpand && <button type="button" className="sgd-builder-text-action" onClick={() => setExpanded(!expanded)}>
                {expanded ? "Collapse" : "Expand"}
              </button>}
            <div>
              <button type="button" className="sgd-builder-text-action" onClick={() => setModal("env")}>Variables</button>
            </div>
          </footer>
        </section>;
    };
    return <section id={DEPLOYMENT_COMPONENT_ID} className="not-prose sg-command-visualizer sgd-command-builder" style={{
      scrollMarginTop: "104px"
    }} aria-label={`${config.modelName} command builder`}>
        <nav className="sgd-builder-scope-tabs" role="tablist" aria-label="Command builder scope">
          {["base", "serve", "request"].map(scope => <button type="button" role="tab" key={scope} aria-label={scopeLabel[scope]} aria-selected={builderScope === scope} aria-controls={`${DEPLOYMENT_COMPONENT_ID}-controls`} data-active={builderScope === scope ? "true" : "false"} onClick={() => setBuilderScope(scope)}>
              {scopeLabel[scope]}
            </button>)}
        </nav>

        <div className="sgd-builder-main" data-scope={builderScope}>
          <div id={`${DEPLOYMENT_COMPONENT_ID}-controls`} className="sgd-builder-controls" role="tabpanel" aria-label={`${scopeLabel[builderScope]} settings`}>
            {renderScopeControls()}
          </div>
          <div className="sgd-builder-output-rail">
            {builderScope !== "request" && renderOutputCard("serve")}
            {builderScope !== "serve" && renderOutputCard("request")}
          </div>
        </div>

        {modal === "env" && <div style={s.modalBackdrop} onClick={() => setModal(null)}>
            <div style={s.modalBox} onClick={event => event.stopPropagation()}>
              <div style={s.modalHeader}>
                <div style={s.modalTitle}>Command variables</div>
                <button style={s.modalCloseBtn} onClick={() => setModal(null)} aria-label="Close">×</button>
              </div>
              {["command", "curl"].map(target => placeholderGroups[target].length > 0 && <div key={target}>
                  <div style={s.sectionHeading}>{target === "command" ? "Serve" : "Request"}</div>
                  {placeholderGroups[target].map(({key, label}) => <div key={key} style={s.formField}>
                      <label style={s.formLabel}>{label}</label>
                      <input style={s.formInput} value={envDraft[key] ?? ""} onChange={event => setEnvDraft({
      ...envDraft,
      [key]: event.target.value
    })} />
                    </div>)}
                </div>)}
              <div style={{
      display: "flex",
      justifyContent: "flex-end",
      gap: 8,
      marginTop: 16
    }}>
                <button style={{
      ...s.iconButton,
      padding: "6px 14px"
    }} onClick={() => setModal(null)}>Cancel</button>
                <button style={s.primaryBtn} onClick={() => {
      saveEnv(envDraft);
      setModal(null);
    }}>Save</button>
              </div>
            </div>
          </div>}
      </section>;
  }
  return <div id={DEPLOYMENT_COMPONENT_ID} style={{
    ...s.container,
    scrollMarginTop: "104px"
  }} className="not-prose sg-command-visualizer">
      {}
      <div style={s.cardColumn}>
        <div style={{
    ...s.title,
    marginBottom: "2px"
  }}>Hardware Platform</div>
        {hwGroups.map(g => <div key={g.label || "hardware"} style={s.vendorRow}>
            {g.label && <div style={s.vendorLabel}>{g.label}</div>}
            <div style={s.itemsGrid(maxHwCols)}>
              {g.items.map(item => renderButton(item, "hw", sel.hw))}
              {Array.from({
    length: maxHwCols - g.items.length
  }).map((_, i) => <div key={`pad-${i}`} />)}
            </div>
          </div>)}
      </div>

      {matchDimSpecs.filter(d => rowVisible(d, sel)).map(d => <div key={d.id}>
            {renderFlatSection(d.title, visibleOptions(d, sel), d.id, sel[d.id])}
          </div>)}
      {overlayDimSpecs.filter(d => rowVisible(d, sel)).map(d => <div key={d.id}>
            {renderFlatSection(d.title, visibleOptions(d, sel), d.id, sel[d.id])}
          </div>)}

      {}
      <div style={s.card}>
        <div style={s.title}>Command:</div>
        <div style={s.commandWrap}>
          {cell && cell.redirect ? cell.warn && <div style={s.mtpWarn}>⚠️ {renderWarn(cell.warn)}</div> : <>
            <div style={s.commandHeader}>
              <div style={s.headerLeft}>
                <div style={s.badge(verifyStatus)}>
                  <span style={s.badgeDot(verifyStatus)} />
                  {VERIFY_LABEL[verifyStatus]}
                </div>
                <div style={s.runModeWrap} role="tablist" aria-label="Output format">
                  {runModes.map((mode, index) => <span key={mode} className="sg-command-visualizer-tab" style={{
    ...index === runModes.length - 1 ? s.runModeChipLast(activeRunMode === mode) : s.runModeChip(activeRunMode === mode),
    ...runModes.length === 1 ? {
      borderRadius: 7
    } : {}
  }} onClick={() => setRunMode(mode)} onKeyDown={e => {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    setRunMode(mode);
  }} role="tab" tabIndex={0} aria-selected={activeRunMode === mode}>
                      {mode === "docker" ? "Docker" : "Python"}
                    </span>)}
                </div>
              </div>
              <div style={s.iconRow}>
                <button style={s.iconButton} onClick={handleCopy}>
                  {copied ? "✓ Copied" : "⧉ Copy"}
                </button>
                <button style={s.iconButton} onClick={() => setModal("curl")}>$ cURL</button>
                <button style={s.iconButton} onClick={() => setModal("env")}>⚙ Env</button>
              </div>
            </div>
            <pre style={s.commandPre}>{command}</pre>
            {cell && cell.warn && <div style={s.mtpWarn}>⚠️ {renderWarn(cell.warn)}</div>}
            {mtpHint && <div style={s.mtpWarn}>
                ⚠️ Speculative decoding ({specAlgoName}) is on — SGLang resets <code>--max-running-requests</code> to <strong>48</strong> when it isn't set. Add <code>--max-running-requests &lt;N&gt;</code> sized for your target concurrency.
              </div>}
            {specPinnedHint && <div style={s.mtpWarn}>
                ℹ️ Speculative decoding ({specAlgoName}) is on and this recipe pins <code>--max-running-requests</code> to <strong>{specMrrValue}</strong>. Adjust it to match your target concurrency — if you remove the flag, SGLang falls back to <strong>48</strong>.
              </div>}
          </>}
        </div>
      </div>

      {}
      {benchmarks && cell && renderBenchmarkCard(benchEntry)}

      {}
      {config.showPlaygroundLink !== false && <div style={{
    padding: "6px 12px",
    fontSize: "12px",
    color: isDark ? "#9ca3af" : "#6b7280",
    display: "flex",
    alignItems: "center",
    gap: "6px"
  }}>
          <span>Need to go beyond the verified matrix?</span>
          <button type="button" onClick={() => {
    const el = document.getElementById("playground");
    if (el) el.scrollIntoView({
      behavior: "smooth",
      block: "start"
    });
  }} style={{
    background: "transparent",
    border: "none",
    padding: 0,
    color: isDark ? "#FDBA74" : "#C2410C",
    cursor: "pointer",
    fontSize: "12px",
    fontWeight: 600,
    textDecoration: "underline",
    textUnderlineOffset: "2px"
  }}>
            Open the Playground →
          </button>
        </div>}

      {}
      {modal === "curl" && <div style={s.modalBackdrop} onClick={() => setModal(null)}>
          <div style={s.modalBox} onClick={e => e.stopPropagation()}>
            <div style={s.modalHeader}>
              <div style={s.modalTitle}>cURL example</div>
              <button style={s.modalCloseBtn} onClick={() => setModal(null)} aria-label="Close">×</button>
            </div>
            <div style={s.commandWrap}>
              <div style={s.commandHeader}>
                <div style={{
    fontSize: 11,
    opacity: 0.7
  }}>
                  Model: <code>{modelName || "(unresolved)"}</code>
                </div>
                <button style={s.iconButton} onClick={copyCurl}>
                  {curlCopied ? "✓ Copied" : "⧉ Copy"}
                </button>
              </div>
              <pre style={s.commandPre}>{curlText}</pre>
            </div>
            <p style={{
    fontSize: 11,
    opacity: 0.7,
    marginTop: 8
  }}>
              Edit <code>CURL_HOST</code> / <code>CURL_PORT</code> in the Env panel.
            </p>
          </div>
        </div>}

      {}
      {modal === "env" && <div style={s.modalBackdrop} onClick={() => setModal(null)}>
          <div style={s.modalBox} onClick={e => e.stopPropagation()}>
            <div style={s.modalHeader}>
              <div style={s.modalTitle}>Env / placeholder values</div>
              <button style={s.modalCloseBtn} onClick={() => setModal(null)} aria-label="Close">×</button>
            </div>
            {placeholderGroups.curl.length > 0 && <div>
                <div style={s.sectionHeading}>cURL placeholders</div>
                {placeholderGroups.curl.map(({key, label}) => <div key={key} style={s.formField}>
                    <label style={s.formLabel}>
                      {label} <code style={{
    opacity: 0.6
  }}>{`{{${key}}}`}</code>
                    </label>
                    <input style={s.formInput} value={envDraft[key] ?? ""} onChange={e => setEnvDraft({
    ...envDraft,
    [key]: e.target.value
  })} />
                  </div>)}
              </div>}
            {placeholderGroups.command.length > 0 && <div>
                <div style={s.sectionHeading}>Command placeholders</div>
                {placeholderGroups.command.map(({key, label}) => <div key={key} style={s.formField}>
                    <label style={s.formLabel}>
                      {label} <code style={{
    opacity: 0.6
  }}>{`{{${key}}}`}</code>
                    </label>
                    <input style={s.formInput} value={envDraft[key] ?? ""} onChange={e => setEnvDraft({
    ...envDraft,
    [key]: e.target.value
  })} />
                  </div>)}
              </div>}
            <div style={{
    display: "flex",
    justifyContent: "flex-end",
    gap: 8,
    marginTop: 16
  }}>
              <button style={{
    ...s.iconButton,
    padding: "6px 14px"
  }} onClick={() => setModal(null)}>Cancel</button>
              <button style={s.primaryBtn} onClick={() => {
    saveEnv(envDraft);
    setModal(null);
  }}>Save</button>
            </div>
            <p style={{
    fontSize: 11,
    opacity: 0.7,
    marginTop: 10
  }}>
              Values persist in localStorage and are reused the next time you visit any cookbook.
            </p>
          </div>
        </div>}

      {}
      {modal === "bench" && benchEntry && (() => {
    const bc = buildBenchCommands(benchEntry, sel);
    if (!bc) return null;
    const selSummary = [sel.hw && sel.hw.toUpperCase(), sel.variant, sel.quant && sel.quant.toUpperCase(), sel.strategy, sel.kvDsaPair, sel.nodes].filter(part => part !== undefined && part !== null && part !== "").join(" · ");
    let selConc = null;
    let speedCmd = null;
    if (bc.speed) {
      selConc = bc.speed.concurrencies.includes(benchConc) ? benchConc : bc.speed.concurrencies[0];
      const w = bc.speed.workload;
      speedCmd = interpolate(bc.speed.template, {
        ...env,
        DATASET: w.dataset,
        ISL: w.isl,
        OSL: w.osl,
        MAX_CONCURRENCY: selConc,
        NUM_PROMPTS: bc.speed.numPromptsOf(selConc)
      }, modelName);
    }
    let selAcc = null;
    let accCmd = null;
    if (bc.accuracy.length > 0) {
      selAcc = bc.accuracy.find(a => a.key === benchAcc) || bc.accuracy[0];
      accCmd = interpolate(selAcc.template, env, modelName);
    }
    return <div style={s.modalBackdrop} onClick={() => setModal(null)}>
            <div style={s.modalBox} onClick={e => e.stopPropagation()}>
              <div style={s.modalHeader}>
                <div style={s.modalTitle}>Benchmark commands</div>
                <button style={s.modalCloseBtn} onClick={() => setModal(null)} aria-label="Close">×</button>
              </div>
              <p style={{
      fontSize: 11,
      opacity: 0.7,
      margin: "0 0 12px"
    }}>
                For <code>{selSummary}</code>. Start the server with the Deploy command above, then run these against it.
              </p>

              {selAcc && <div>
                  <div style={s.sectionHeading}>Accuracy</div>
                  {bc.accuracy.length > 1 && <div style={s.benchChipRow}>
                      <span style={{
      fontSize: 11,
      opacity: 0.7
    }}>benchmark:</span>
                      {bc.accuracy.map(a => <button key={a.key} style={{
      ...s.benchChip,
      ...a.key === selAcc.key ? s.benchChipActive : {}
    }} onClick={() => setBenchAcc(a.key)}>
                          {a.label}
                        </button>)}
                    </div>}
                  <div style={{
      ...s.commandWrap,
      marginBottom: 6
    }}>
                    <div style={s.commandHeader}>
                      <div style={{
      fontSize: 11,
      opacity: 0.7
    }}>{selAcc.label}</div>
                      <button style={s.iconButton} onClick={() => copyBench("acc", accCmd)}>
                        {benchCopied === "acc" ? "✓ Copied" : "⧉ Copy"}
                      </button>
                    </div>
                    <pre style={s.commandPre}>{accCmd}</pre>
                  </div>
                  {bc.accuracy.length > 1 && <p style={{
      fontSize: 11,
      opacity: 0.7,
      margin: "0 0 4px"
    }}>
                      Switch the benchmark chip to see each eval's command.
                    </p>}
                </div>}

              {bc.speed && <div>
                  <div style={s.sectionHeading}>Speed</div>
                  {bc.speed.concurrencies.length > 1 && <div style={s.benchChipRow}>
                      <span style={{
      fontSize: 11,
      opacity: 0.7
    }}>max-concurrency:</span>
                      {bc.speed.concurrencies.map(c => <button key={c} style={{
      ...s.benchChip,
      ...c === selConc ? s.benchChipActive : {}
    }} onClick={() => setBenchConc(c)}>
                          {c}
                        </button>)}
                    </div>}
                  <div style={{
      ...s.commandWrap,
      marginBottom: 6
    }}>
                    <div style={s.commandHeader}>
                      <div style={{
      fontSize: 11,
      opacity: 0.7
    }}>max-concurrency = {selConc}</div>
                      <button style={s.iconButton} onClick={() => copyBench("speed", speedCmd)}>
                        {benchCopied === "speed" ? "✓ Copied" : "⧉ Copy"}
                      </button>
                    </div>
                    <pre style={s.commandPre}>{speedCmd}</pre>
                  </div>
                  <p style={{
      fontSize: 11,
      opacity: 0.7,
      margin: "0 0 4px"
    }}>
                    One command — switch the concurrency chip (or edit <code>--max-concurrency</code>) to reproduce each Speed column.
                  </p>
                </div>}

              <p style={{
      fontSize: 11,
      opacity: 0.7,
      marginTop: 12
    }}>
                Edit <code>CURL_HOST</code> / <code>CURL_PORT</code> in the Env panel.
              </p>
            </div>
          </div>;
  })()}
    </div>;
};

export const DiffusionModelTags = ({tags = []}) => {
  const normalizedTags = Array.isArray(tags) ? tags : [tags];
  return <div className="not-prose sgd-model-tags">
      {normalizedTags.map(tag => <span key={tag} className="sgd-chip">
          {tag}
        </span>)}
    </div>;
};
```
