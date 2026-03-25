from __future__ import annotations

from typing import Any
import re


def clean_text(value: Any) -> str:
    return (value or "").strip()


def get_primary_group_name(system) -> str:
    # Keep identical behavior to app_main.py
    return system.primary_system_name or system.identifier


def extract_hardware_component(hardware_string: str, component_prefix: str) -> str | None:
    """Extract a specific component like 'Processor: ' from the Phoronix hardware string."""
    if not hardware_string:
        return None
    for part in hardware_string.split(","):
        part = part.strip()
        if part.startswith(f"{component_prefix}:"):
            return part.split(":", 1)[1].strip()
    return None


def normalize_processor_name(processor: str) -> str:
    """
    Shorten CPU model strings for labels and for matching `HardwareTheoreticalRank.match_key`.

    Example: "AMD Ryzen 9 9950X 16-Core @ 5.76GHz ..." -> "AMD Ryzen 9 9950X"
    """
    s = clean_text(processor)
    if not s:
        return ""
    s = re.split(r"\s*@\s*", s, maxsplit=1)[0].strip()
    s = re.sub(r"\s*\d+\s*-\s*Core[s]?\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\d+\s*-\s*Core\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\d+\s*Core[s]?\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_graphics_name(graphics: str) -> str:
    """
    Shorten GPU strings for matching theoretical ranks (VRAM, Laptop GPU suffix, etc.).
    """
    s = clean_text(graphics)
    if not s:
        return ""
    s = re.split(r"\s*@\s*", s, maxsplit=1)[0].strip()
    s = re.sub(r"\s*\d+\s*GB\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\d+\s*MB\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+Laptop\s+GPU\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def hardware_rank_match_key(feature_key: str, display_value: str) -> str:
    """Normalize component label for `HardwareTheoreticalRank` lookup."""
    fk = (feature_key or "").strip().lower()
    if fk == "processor":
        return normalize_processor_name(display_value)
    if fk == "graphics":
        return normalize_graphics_name(display_value)
    return clean_text(display_value)


def extract_software_component(software_text: str, label: str) -> str:
    """Extract value for a label from Phoronix-style software string (e.g. 'Kernel: 6.8.0' or 'NVIDIA Driver: 560')."""
    if not software_text or not label:
        return ""
    # Split on comma and newline so we handle both "A: 1, B: 2" and "A: 1\nB: 2"
    for part in (software_text.replace("\n", ",").split(",")):
        part = part.strip()
        if part.lower().startswith(label.lower() + ":"):
            return part.split(":", 1)[1].strip()
    return ""


def get_system_components(system) -> dict[str, str]:
    """Build a dict of component keys -> display values for comparison labels (CPU, GPU, OS, etc.)."""
    hardware = system.hardware or ""

    def extract_hw_any(prefixes: list[str]) -> str:
        for p in prefixes:
            v = extract_hardware_component(hardware, p)
            if v:
                return v
        return ""

    # Parsed from hardware string (Phoronix labels vary a bit between sources)
    processor = extract_hw_any(["Processor", "CPU", "CPU Model"])
    processor = normalize_processor_name(processor) if processor else ""
    graphics = extract_hw_any(["Graphics", "GPU", "Graphics Processor"])
    graphics = normalize_graphics_name(graphics) if graphics else ""
    memory = extract_hw_any(["Memory", "RAM", "System Memory"])
    motherboard = extract_hw_any(["Motherboard", "Mainboard", "Motherboard / Mainboard"])
    chipset = extract_hw_any(["Chipset"])

    software = (system.software or "").strip()

    # OS: first non-empty line, or first "OS:" value, or "Unknown"
    os_val = extract_software_component(software, "OS")
    if not os_val:
        os_val = (software.split("\n")[0] or "").strip() if software else ""
    if not os_val:
        os_val = "Unknown" if software else ""

    # Software version fields (common in Phoronix / hardware insights)
    kernel_version = extract_software_component(software, "Kernel")
    nvidia_driver = extract_software_component(software, "NVIDIA Driver")
    if not nvidia_driver and graphics and "nvidia" in (graphics or "").lower():
        nvidia_driver = extract_software_component(software, "Driver")
    mesa_version = extract_software_component(software, "Mesa") or extract_software_component(software, "Mesa 3D")
    llvm_version = extract_software_component(software, "LLVM")
    vulkan_driver = extract_software_component(software, "Vulkan")

    # Profile fields
    chassis_version = clean_text(system.chassis_version) or ""
    cooler_model = clean_text(system.cooler_model) or ""
    psu = " ".join(part for part in [clean_text(system.psu_wattage), clean_text(system.psu_model)] if part).strip()
    custom_hardware = clean_text(system.custom_hardware) or ""
    external_off = "Yes" if system.external_off else "No"
    gpu_fans = "Yes" if system.gpu_fans else "No"
    memory_fans = "Yes" if system.memory_fans else "No"
    nvme_fans = "Yes" if system.nvme_fans else "No"

    # NVMe thermal pads (any drive)
    top_pad = any(c.top_thermal_pad for c in (system.nvme_configs or []))
    bottom_pad = any(c.bottom_thermal_pad for c in (system.nvme_configs or []))
    thermal_pad_above_nvme = "Yes" if top_pad else "No"
    thermal_pad_below_nvme = "Yes" if bottom_pad else "No"
    thermal_pad_sandwich_nvme = "Yes" if top_pad and bottom_pad else "No"

    return {
        "system_name": get_primary_group_name(system),
        "identifier": clean_text(system.identifier) or "",
        "processor": processor or "",
        "graphics": graphics or "",
        "memory": memory or "",
        "motherboard": motherboard or "",
        "chipset": chipset or "",
        "os": os_val,
        "kernel_version": kernel_version or "",
        "nvidia_driver": nvidia_driver or "",
        "mesa_version": mesa_version or "",
        "llvm_version": llvm_version or "",
        "vulkan_driver": vulkan_driver or "",
        "chassis_version": chassis_version,
        "cooler_model": cooler_model,
        "psu": psu,
        "custom_hardware": custom_hardware,
        "external_off": external_off,
        "gpu_fans": gpu_fans,
        "memory_fans": memory_fans,
        "nvme_fans": nvme_fans,
        "thermal_pad_above_nvme": thermal_pad_above_nvme,
        "thermal_pad_below_nvme": thermal_pad_below_nvme,
        "thermal_pad_sandwich_nvme": thermal_pad_sandwich_nvme,
    }

