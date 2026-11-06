# Qwen/Qwen-Image-2.1 — vLLM serving recipe

> Unified text-to-image and image-conditioned generation. A 7.1B single-stream DiT with block-causal
> attention and an exact cross-step prefix KV cache, paired with a Qwen3-VL-8B text encoder and a
> 16x RGBA autoencoder.

> Saved locally from https://recipes.vllm.ai/Qwen/Qwen-Image-2.1.md on 2026-11-06. The page renders one configuration per URL, so
> the body below is the export for `hardware=gb300 · variant=default · task=t2i`.
> `task=i2i` was fetched as well (`?hardware=gb300&task=i2i`) and is byte-identical apart from three
> metadata lines (the Configuration line and the two page/JSON links); the Serve command does not vary
> with task, so only the t2i export is stored here.
> Not captured: `variant=fp8` and the other hardware values (`h100`, `h200`, `b200`, `gb200`, `b300`,
> `gb300`, `mi300x`, `mi325x`, `mi350x`, `mi355x`) — fetch them by changing the query string. Note that
> this recipe has no consumer-GPU profile at all (no RTX 5090 / 4090 entry).
> 本地核对到的口径差异（不改动正文）：
> - 本页 Install 段仍写“vLLM-Omni 必须从 PR #7759 构建、main 上没有”，而
>   `raw.githubusercontent.com/.../main/recipes/Qwen/Qwen-Image-2.1.md` 已可直接取到该 recipe
>   （本目录的 in-repo 副本即来自 main）；以 in-repo 为准，本页该段视为滞后口径。
> - 本页写 `true_cfg_scale` 默认 4.0，in-repo 参数表写 1.0。
> - 本页推荐 40 步（并说服务端默认 50），in-repo 示例用 50 步。

Configuration: hardware `gb300` (4× GB300 NVL4) · variant `default` (BF16) · task `t2i`

- Interactive page: https://recipes.vllm.ai/Qwen/Qwen-Image-2.1
- This configuration as JSON: https://recipes.vllm.ai/Qwen/Qwen-Image-2.1.json?hardware=gb300

The commands below were rendered for exactly this configuration and take precedence over any example in the guide.

## Other configurations

Change the query string (the same parameters as the page URL) and fetch this URL again. Unknown or incompatible values fall back to the recipe default, so check the Configuration line.

- `hardware`: `h100`, `h200`, `b200`, `gb200`, `b300`, `gb300`, `mi300x`, `mi325x`, `mi350x`, `mi355x`
- `variant`: `default`, `fp8`
- `task`: `t2i`, `i2i`

## Install

Requires vLLM >= 0.29.0.

Extra dependencies:

- vLLM-Omni must be built from PR #7759. Qwen-Image-2.1 is not in a tagged release and not on main either, so a plain clone cannot serve it.

```bash
git clone https://github.com/vllm-project/vllm-omni.git && cd vllm-omni && git fetch origin pull/7759/head:qwen-image-2.1 && git checkout qwen-image-2.1 && uv pip install -e .
```

## Serve

vLLM-Omni recipe — the generation endpoints and request examples are in the guide below.

```bash
vllm serve Qwen/Qwen-Image-2.1 \
  --omni
```

## Guide

> Background reference. Commands in it are examples and may target another configuration — use the Serve section above.

### Overview

vLLM-Omni accelerates Qwen-Image 2.1 through cross-step prefix KV cache reuse and dedicated CUDA Graphs, reducing redundant computation and kernel launch overhead. Request-level and step-level continuous batching improve GPU utilization and throughput, with phase-aware prefill and decode scheduling. It also supports tensor and Ulysses sequence parallelism, distributed VAE decoding with adaptive OOM recovery, FP8 weights and prefix KV storage, and CPU offloading for varying memory budgets.

One pipeline class, `QwenImage21Pipeline`, serves both text-to-image and
image-conditioned generation (editing). Prompt and reference images are encoded
together by a Qwen3-VL vision-language model, so an edit request takes the same
serving path as plain text-to-image, and there is no separate edit model to load.

| Component | Class | On disk |
|---|---|---|
| Text encoder | `Qwen3VLForConditionalGeneration` | bf16, 17.5 GB |
| DiT | `QwenImage21Transformer2DModel` | bf16, 14.2 GB |
| Autoencoder | `AutoencoderKLQwenImage21` | fp32, 1.4 GB |

The autoencoder compresses 16x into a 64-channel latent and carries 4 channels in
and out, so RGBA transparency survives the round trip.

### Sampling defaults

Sample at **40 steps with classifier-free guidance off** — `num_inference_steps=40`
and `true_cfg_scale=1.0` in the reference implementation's pipeline signature
([diffusers#14804](https://github.com/huggingface/diffusers/pull/14804)).

```bash
--num-inference-steps 40 --cfg-scale 1.0
```

`--cfg-scale` above 1 engages only with a negative prompt; otherwise it is ignored
with a warning. When it engages, the DiT runs twice per step, roughly doubling
latency.

### Prerequisites

This source checkout includes the pipeline and targets final vLLM 0.33.0.
Full image-quality qualification remains pending; successful generation alone
does not establish reference parity.

Historical bring-up from PR #7759 on one NVIDIA GB300 generated a 1024x1024
PNG in **4.5 s** over 40 steps at **34.0 GB** peak. Those measurements do not
qualify the current source/runtime.

```bash
git clone https://github.com/vllm-project/vllm-omni.git && cd vllm-omni
uv venv --python 3.12 --seed && source .venv/bin/activate
uv pip install vllm==0.33.0 --torch-backend=cu130 && uv pip install -e .
```

### Offline inference

```bash
# text-to-image
python examples/offline_inference/text_to_image/text_to_image.py \
  --model Qwen/Qwen-Image-2.1 \
  --prompt "A ceramic teapot on a wooden table" \
  --output t2i.png --num-inference-steps 40 --cfg-scale 1.0

# image-conditioned
python examples/offline_inference/image_to_image/image_edit.py \
  --model Qwen/Qwen-Image-2.1 --color-format RGBA --seed 42 \
  --image input1.png input2.png \
  --prompt "Combine these images into a single scene" \
  --output edit.png --num-inference-steps 40 --cfg-scale 1.0
```

Up to 4 reference images per request; a fifth is rejected with a 400. Each is resized
independently to ~1024x1024 of area, so they may differ in aspect ratio, and each
is tagged `<image1>`, `<image2>` in the order given, so an ordinal reference in the
prompt binds a phrase to a photo.

`--color-format RGBA` preserves transparency (the shared example defaults to RGB).
Width and height are floored to a multiple of 32, never rejected. When omitted on an edit request
the output size derives from the last reference image's aspect ratio at ~1024x1024.

### Online serving

```bash
vllm serve Qwen/Qwen-Image-2.1 --omni --port 8091
```

Add `--step-execution --max-num-seqs 8` to batch requests at the same KV-cache
phase. Admission is all-or-nothing: once a wave is past its first denoising step, a
new arrival waits for it to finish, even if `max-num-seqs` has room.

Send `num_inference_steps` on every request; the server's own default is 50, not
the 40 this checkpoint wants. `true_cfg_scale` defaults to 4.0 but only applies
alongside a `negative_prompt` — send one without the other and you get guidance
at 4.0 and roughly double the compute.

#### Text to image

Send JSON to `/v1/images/generations`. The picture comes back base64-encoded at
`.data[0].b64_json`.

```bash
curl -X POST http://localhost:8091/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen-Image-2.1",
       "prompt": "A ceramic teapot on a wooden table",
       "size": "1024x1024",
       "num_inference_steps": 40,
       "true_cfg_scale": 1.0,
       "seed": 42}'
```

#### Image editing

Send a **form**, not JSON, to `/v1/images/edits`. The picture is a file upload
and everything else is a form field. A JSON body to this endpoint resets the
connection. Repeat `-F image=@...` for more pictures, up to four.

```bash
curl -X POST http://localhost:8091/v1/images/edits \
  -F image=@plate.png \
  -F 'prompt=Write the words "FRESH BASIL" on this plate in dark green lettering' \
  -F model=Qwen/Qwen-Image-2.1 \
  -F size=1024x1024 \
  -F num_inference_steps=40 \
  -F true_cfg_scale=1.0 \
  -F seed=42
```

The reply has the same shape as text to image: the picture is at
`.data[0].b64_json`.

If you would rather send JSON, the chat endpoint also accepts pictures. It puts
the result somewhere different — `.choices[0].message.content[0].image_url.url`:

```bash
curl -X POST http://localhost:8091/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen-Image-2.1",
       "messages": [{"role": "user", "content": [
         {"type": "image_url", "image_url": {"url": "data:image/png;base64,<BASE64>"}},
         {"type": "text", "text": "Write the words \"FRESH BASIL\" on this plate"}
       ]}],
       "modalities": ["image"],
       "extra_body": {"size": "1024x1024", "num_inference_steps": 40,
                      "true_cfg_scale": 1.0, "seed": 42}}'
```

### Prefix KV cache

On automatically; rebuilt per generation, so nothing leaks between requests. The
prompt and any reference images are cached after the first denoising step and
reused for the rest, which is exact rather than approximate.

It pays off in proportion to how much prefix there is: repeating a prompt costs
about a third of the first submission, four reference images leave only about a
fifth of the sequence to recompute, and plain short-prompt text-to-image has
almost nothing to cache. Changing the tail of a prompt loses the saving.

For long-prompt or multi-image workloads it can be stored in FP8 via the stage
`extras` key `prefix_kv_cache_dtype` — on the command line through
`--stage-overrides '{"0":{"extras":{"prefix_kv_cache_dtype":"fp8_v"}}}'`, or as a
stage field in a deploy YAML. `"fp8_v"` quantizes V only, `"fp8"` quantizes K too — the contributors
measured ~41 dB and ~35 dB PSNR against BF16. A quantized cache returns less of
the speed-up than one in the native dtype, and is not CUDA-graph capturable, so
those requests fall back to eager decode.

### CUDA graphs

Fixed-shape decode steps are captured automatically (`enforce_eager=False` by
default); prefill stays eager. Each image layout gets its own entry and prefix K/V
is copied into static buffers before replay, which costs memory. TP, SP/ring, HSDP,
offload and cache hooks, compiled blocks, quantized KV caches, dynamic LoRA and
padded text masks each force eager decode. Disable with `--enforce-eager`; the AR
engine's `compilation_config.cudagraph_mode` does not reach this path.

Expect the first request after startup to cost about 1.3x a later one.
`--enforce-eager` does not remove that cost, so it is not graph capture and
disabling graphs will not flatten startup.

### Quantization

Online FP8, per component:

```python
from vllm_omni import Omni
omni = Omni(model="Qwen/Qwen-Image-2.1", quantization_config={
    "transformer": {"method": "fp8", "ignored_layers": ["img_mlp"]},
    "text_encoder": {"method": "fp8"},
})
```

Only the DiT's block-internal linears are eligible; boundary projections stay
BF16. Measured by the vLLM-Omni contributors on GB200 at 1024x1024, 50 steps:

| Config | Quantized | Avg PSNR vs BF16 | Peak memory |
|---|---|---|---|
| BF16 | 0 / 160 | — | 40.0 GB |
| FP8, all layers | 160 / 160 | 26.1 dB | 33.4 GB |
| FP8, `ignored_layers=["img_mlp"]` | 64 / 160 | 29.7 dB | 38.3 GB |

Measured here on one GB300 at 1024x1024, 40 steps, same prompt and seed in each
case:

| Config | Peak memory | Time per image |
|---|---|---|
| BF16 | 34.0 GB | 3282-4492 ms |
| DiT FP8, `img_mlp` kept BF16 | 32.0-32.7 GB | 3356-4706 ms |
| Text-encoder FP8 | **27.5-28.1 GB** | 3427-4770 ms |

The text encoder is the bigger saving by a wide margin: about 6 GB against about
2 GB. Neither runs faster.

Keeping `img_mlp` in BF16 protects composition but gives back most of the saving.
The larger win is the text encoder: in a separate GB200 run, peak memory went ~41.0
GiB in BF16 to ~34.4 GiB with text-encoder FP8, and ~28.1 GiB with the DiT too. Its
vision tower and unused `lm_head` are excluded by construction, but reference-image
tokens still cross the quantized language-model linears, so text-encoder FP8 does
reach the edit path. On Blackwell FP8 buys memory, not speed.

### Limitations

- Cache backends (`cache_dit`, `tea_cache`) unsupported — the model's own prefix
  cache conflicts with step-skipping hooks.
- Sequence parallelism is Ulysses only — no ring attention, no pipeline parallelism.
- True CFG only; there is no `guidance_scale`.
- `/v1/images/edits` takes **multipart/form-data**, not JSON. Send the picture
  as a file upload and the settings as form fields. Posting a JSON body to it
  resets the connection, which is easy to mistake for a broken endpoint.
- Passing a picture as a `data:` URL in the `url` form field is limited to
  **1024 KB per part**. A 512x512 PNG from this model is about 1 MB, so it
  usually fails with `Part exceeded maximum size of 1024KB`. File uploads have
  no such limit. Base64 inside a form field only suits small images.
- `/v1/chat/completions` with `image_url` content parts works too, and returns
  the picture at `.choices[0].message.content[0].image_url.url`.
- NVIDIA GPUs only.
- VAE tiling (`--vae-use-tiling`) decodes in 512px tiles at 384px stride, halving
  on CUDA OOM down to 128px; tile sizes are not exposed as engine arguments.

### References

- [In-repo recipe](https://github.com/vllm-project/vllm-omni/blob/qwen-image-2.1/recipes/Qwen/Qwen-Image-2.1.md)
- [Feature support matrix](https://github.com/vllm-project/vllm-omni/blob/qwen-image-2.1/docs/user_guide/diffusion_features.md)
