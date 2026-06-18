"""Workload characterization constants — scope maps, perf counter markers, aliases."""

from __future__ import annotations

SCOPE_HARDWARE_KEYS: dict[str, frozenset[str]] = {
    "cpu": frozenset({
        "processor", "memory", "motherboard", "chipset", "os", "kernel_version",
        "llvm_version", "cooler_model", "chassis_version", "psu", "custom_hardware",
        "external_off", "memory_fans",
    }),
    "gpu": frozenset({
        "graphics", "nvidia_driver", "mesa_version", "llvm_version", "vulkan_driver",
        "processor", "memory", "os", "chassis_version", "gpu_fans",
    }),
    "storage": frozenset({
        "nvme_fans", "thermal_pad_above_nvme", "thermal_pad_below_nvme",
        "thermal_pad_sandwich_nvme", "custom_hardware", "chassis_version", "processor",
    }),
    "memory": frozenset({
        "processor", "memory", "motherboard", "chipset", "cooler_model", "memory_fans",
        "chassis_version", "psu",
    }),
    "general": frozenset({
        "processor", "graphics", "memory", "motherboard", "chipset", "os",
        "kernel_version", "nvidia_driver", "mesa_version", "llvm_version",
        "vulkan_driver", "chassis_version", "cooler_model", "psu", "custom_hardware",
        "external_off", "gpu_fans", "memory_fans", "nvme_fans",
    }),
}

SCOPE_SENSOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "cpu": (
        "cpu temp", "cpu temperature", "cpu freq", "cpu frequency", "cpu usage",
        "cpu power", "cpu util", "energy-cores", "energy-pkg", "package power",
    ),
    "gpu": (
        "gpu temp", "gpu temperature", "gpu freq", "gpu frequency", "gpu usage",
        "gpu power", "gpu util", "energy-gpu", "graphics",
    ),
    "storage": ("nvme", "disk", "ssd", "storage", "read", "write i/o"),
    "memory": ("memory", "ram", "swap", "dimm"),
    "general": (
        "cpu temp", "cpu temperature", "cpu freq", "cpu frequency", "cpu usage",
        "cpu power", "gpu temp", "gpu temperature", "gpu freq", "gpu frequency",
        "gpu usage", "gpu power", "memory", "ram", "nvme", "energy",
    ),
}

_PERF_MARKERS = ("perf ", "perf-", "perf/")

_COUNTER_ALIASES: list[tuple[str, str]] = [
    ("instructions", "instructions"),
    ("cycles", "cycles"),
    ("cache-references", "cache_references"),
    ("cache-misses", "cache_misses"),
    ("branch-instructions", "branch_instructions"),
    ("branch-misses", "branch_misses"),
    ("context-switches", "context_switches"),
    ("cpu-migrations", "cpu_migrations"),
    ("page-faults", "page_faults"),
    ("energy-pkg", "energy_pkg"),
    ("energy-cores", "energy_cores"),
    ("energy-gpu", "energy_gpu"),
]

_BOTTLENECK_HARDWARE_SCOPE: dict[str, str] = {
    "cache": "cpu",
    "thermal": "cpu",
}
