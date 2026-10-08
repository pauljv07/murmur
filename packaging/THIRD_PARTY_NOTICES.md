# Third-party notices

## uv (bundled in Murmur.app)

Murmur.app includes the `uv` binary from Astral (https://github.com/astral-sh/uv), used to create
Murmur's private Python environment. uv is dual-licensed under MIT or Apache-2.0; its MIT license:

```
MIT License

Copyright (c) 2025 Astral Software Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Downloaded at runtime (not bundled)

Python packages listed in `requirements.lock` (installed from PyPI on first launch) and the models
the user selects (downloaded from Hugging Face): NVIDIA Parakeet TDT 0.6B v2/v3 (CC-BY-4.0),
NVIDIA Streaming Sortformer 4spk v2.1 (NVIDIA Open Model License), Qwen3 models via
mlx-community (Apache-2.0). Each is subject to its own license.

## Design

The UI's visual style is modelled on the llama.cpp web UI (MIT License, © The ggml authors).
