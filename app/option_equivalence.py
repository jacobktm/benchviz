from __future__ import annotations

import re


GPU_BACKEND_POOL_TOKEN = "GPU backend (pooled: CUDA/OptiX/HIP/ROCm)"


_RESOLUTION_WIDTH_RE = re.compile(r"\b(?P<w>\d{3,4})\s*[xX×]\s*(?P<h>\d{3,4})\b")
_ASPECT_RE = re.compile(r"\b(?P<w>\d{1,2})\s*[:/]\s*(?P<h>\d{1,2})\b")


def _resolution_class_key(args_str: str) -> str | None:
    """
    Coarse resolution bucketing so "4k 16:9" and "4k 16:10" land in the same pool.
    """
    if not args_str:
        return None

    s = str(args_str)
    sl = s.lower()

    # Common shorthand.
    if re.search(r"\b8k\b", sl):
        return "Resolution class: 8k"
    if re.search(r"\b4k\b", sl):
        return "Resolution class: 4k"
    if re.search(r"\b1440p\b", sl):
        return "Resolution class: 1440p"
    if re.search(r"\b1080p\b", sl):
        return "Resolution class: 1080p"

    m = _RESOLUTION_WIDTH_RE.search(s)
    if not m:
        return None

    w = int(m.group("w"))
    # Heuristic: group by pixel width class.
    if 3800 <= w <= 4100:
        return "Resolution class: 4k"
    if 1500 <= w <= 2000:
        # Covers 1920x1080 and similar
        return "Resolution class: 1080p-ish"
    if 2400 <= w <= 3000:
        # Covers 2560x1440-ish
        return "Resolution class: 1440p-ish"

    return None


def _backend_pool_token(args_str: str) -> str | None:
    if not args_str:
        return None
    s = str(args_str).lower()
    has_cuda = "cuda" in s
    has_optix = "optix" in s
    has_hip = "hip" in s or "rocm" in s
    # "nvidia" / "amd" by themselves can be ambiguous; we only pool when actual
    # backend tokens are present.
    if (has_cuda or has_optix) and (has_cuda or has_optix or has_hip):
        # If we see CUDA/OptiX tokens, we consider them part of the pooled family.
        return GPU_BACKEND_POOL_TOKEN
    if has_hip:
        return GPU_BACKEND_POOL_TOKEN
    return None


def _canonicalize_args_for_pool(args_str: str) -> tuple[str, bool]:
    """
    Create a canonical args string so "equivalent" configs can share one chart axis.

    Returns (canonical_args, changed_flag). changed_flag is true when we recognized
    and normalized known tokens (backend/resolution).
    """
    if args_str is None:
        return "", False

    raw = str(args_str)
    if not raw.strip():
        return raw, False

    sl = raw.lower()
    out = raw.strip()
    changed = False

    # Pool backend keywords by replacing with one token.
    backend_token = _backend_pool_token(out)
    if backend_token:
        # Replace backend tokens individually so multi-arg strings get normalized.
        out2 = re.sub(r"\bcuda\b", backend_token, out, flags=re.IGNORECASE)
        out2 = re.sub(r"\boptix\b", backend_token, out2, flags=re.IGNORECASE)
        out2 = re.sub(r"\bhip\b", backend_token, out2, flags=re.IGNORECASE)
        out2 = re.sub(r"\brocm\b", backend_token, out2, flags=re.IGNORECASE)
        # Reduce repeated insertion.
        out2 = re.sub(r"(GPU backend \(pooled: CUDA/OptiX/HIP/ROCm\))(?:\s*\1)+", r"\1", out2)
        out = out2
        changed = True

    # Normalize resolution into a class token (so 4k 16:9 and 16:10 group).
    res_key = _resolution_class_key(out)
    if res_key:
        # Remove explicit width×height if present; replace with the class token.
        out2 = _RESOLUTION_WIDTH_RE.sub(res_key, out)
        # If only "4k" shorthand was present, keep it but ensure canonical form.
        out2 = re.sub(r"\b4k\b", res_key, out2, flags=re.IGNORECASE)
        out = out2
        changed = True

    # Final cleanup: collapse whitespace.
    out = re.sub(r"\s+", " ", out).strip()
    return out, changed


def pool_key_for_args(benchmark_title: str | None, args_str: str | None) -> str | None:
    """
    Return a canonical "pool key" for args_str so multiple configs can be compared
    on the same axis even when option labels differ.

    This is intentionally generic (not Blender-specific): it canonicalizes
    (1) GPU backend tokens (CUDA/OptiX/HIP/ROCm) and
    (2) coarse resolution classes (e.g. 4k).
    """
    if not args_str or not str(args_str).strip():
        return None

    canonical, changed = _canonicalize_args_for_pool(args_str)
    if not changed:
        return None
    # For UX/debugging, keep original benchmark title out of the key; pooling is per-benchmark
    # in api_compare via the (title, app_version, pool_key) task key anyway.
    return canonical

