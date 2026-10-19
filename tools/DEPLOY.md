# Tools

Machine-local tool installs shared by every project. Everything in `tools/`
except this file is gitignored: binaries and models are downloaded per machine,
and this file is the manifest for rebuilding them.

Projects should call these tools from here (or from PATH) instead of
vendoring their own copy.

## whisper.cpp

Speech-to-text, CPU build with OpenBLAS.

| Item    | Value |
| ------- | ----- |
| Version | 1.9.5 (release `b5454`) |
| Binary  | `tools/whisper/bin/whisper-cli.exe` (also `whisper-server.exe`, `whisper-stream.exe`) |
| Model   | `tools/whisper/models/ggml-large-v3.bin` (~3.1 GB) |
| PATH    | `tools/whisper/bin` is on the user PATH |
| Env     | `WHISPER_MODEL` points at the large-v3 model |

Install (Windows, from the repo root):

```bash
mkdir -p tools/whisper/models && cd tools/whisper
curl -L -o w.zip https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-blas-bin-x64.zip
unzip -oq w.zip && rm w.zip && mv Release bin
curl -L -o models/ggml-large-v3.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin
```

Then add `tools/whisper/bin` to the user PATH and set `WHISPER_MODEL` to the
model's absolute path.

Use:

```bash
whisper-cli -m "$WHISPER_MODEL" -l auto -f input.wav -oj
```

Input must be 16 kHz WAV: `ffmpeg -i in.mp3 -ar 16000 -ac 1 in.wav`.

Prebuilt Windows releases ship CPU, BLAS and CUDA builds only. This machine has
an AMD GPU, so GPU acceleration needs a local Vulkan build
(`-DGGML_VULKAN=ON`, requires the Vulkan SDK).

## Reverse-engineering tools (provided by REA)

Ghidra, rizin, jadx, apktool, jadx-headless-mcp, wabt and the other RE tools
are installed by the REA (reverse-engineer-anything) setup and wired into its
MCP config. Do not deploy them here and do not vendor copies into projects;
point projects at the REA-installed tools instead.

## Global prerequisites (installed outside this folder)

Not managed here, listed so a new machine has everything:

- ffmpeg (winget `Gyan.FFmpeg`)
- Python 3.12, uv (winget `astral-sh.uv`)
- yt-dlp (pip, global Python)
- Java 21 (Eclipse Adoptium)
- .NET SDK 8 (`C:\Program Files\dotnet`)
