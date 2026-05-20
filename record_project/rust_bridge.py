"""
rust_bridge.py
--------------
Python wrapper for calling the Rust vinyl record generation backend.

This module manages subprocess communication with the compiled Rust binary,
handling JSON serialization for groove calculation and STL generation.

Note: numpy is optional. If not available, the module will still work but
      will be slower when processing large audio arrays.
"""

from __future__ import annotations

import json
import subprocess
import os
from pathlib import Path
from typing import Tuple, List, Dict, Any, Optional, Union

# Try to import numpy (optional)
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    np = None


class RustBridge:
    """Interface to the Rust vinyl record generation engine."""
    
    def __init__(self):
        """Initialize the bridge and verify Rust binary exists."""
        rust_dir = Path(__file__).parent / "Rust"
        self.binary_path = rust_dir / "target" / "release" / "vinyl-groove"
        if not self.binary_path.exists():
            debug_path = rust_dir / "target" / "debug" / "vinyl-groove"
            if debug_path.exists():
                self.binary_path = debug_path
            else:
                raise FileNotFoundError(
                    f"Rust binary not found at {self.binary_path}. "
                    "Run: cargo build --release in the Rust/ directory"
                )
    
    def _call_rust(self, command: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Call the Rust binary with a JSON request and return JSON response.
        
        Args:
            command: Command name (e.g., "calculate_groove")
            params: Parameters dictionary
            
        Returns:
            Response dictionary from Rust
        """
        request = {"command": command}
        request.update(params)
        request_json = json.dumps(request).encode('utf-8')
        
        try:
            result = subprocess.run(
                [str(self.binary_path)],
                input=request_json,
                capture_output=True,
                timeout=300,
                check=False
            )
            
            if result.returncode != 0:
                raise RuntimeError(
                    f"Rust binary failed: {result.stderr.decode('utf-8')}"
                )
            
            response = json.loads(result.stdout.decode('utf-8'))
            
            if not response.get('ok'):
                raise RuntimeError(f"Rust error: {response.get('error', 'Unknown error')}")
            
            return response
            
        except subprocess.TimeoutExpired:
            raise TimeoutError("Rust calculation timed out (300s)")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse Rust output: {e}")
    
    def calculate_groove(
        self,
        left: Union['np.ndarray', List[float]],
        right: Union['np.ndarray', List[float]],
        size_inch: int,
        rpm: int,
        groove_mode: str = "stereo",
        quality: str = "high",
        sample_rate: int = 44100,
    ) -> List[Dict[str, float]]:
        """
        Calculate groove path from stereo audio.
        
        Args:
            left: Left channel samples (numpy array or list, normalized to [-1, 1])
            right: Right channel samples (numpy array or list)
            size_inch: Vinyl size (7, 10, 12)
            rpm: Speed (33, 45, 78)
            groove_mode: "mono" or "stereo"
            quality: "preview", "draft", "high", "full", "max"
            sample_rate: Audio sample rate in Hz
            
        Returns:
            List of GroovePoint dicts with x, y, z_floor, x_left, y_left, x_right, y_right
        """
        # Convert to native Python lists if needed
        if HAS_NUMPY and hasattr(left, 'tolist'):
            # numpy array
            left_list = left.astype(np.float32).tolist()
            right_list = right.astype(np.float32).tolist()
        else:
            # already a list or list-like
            left_list = list(left)
            right_list = list(right)
        
        # Clip to [-1, 1] range for safety
        left_list = [max(-1.0, min(1.0, s)) for s in left_list]
        right_list = [max(-1.0, min(1.0, s)) for s in right_list]
        
        params = {
            "size_inch": size_inch,
            "rpm": rpm,
            "groove_mode": groove_mode,
            "quality": quality,
            "sample_rate": sample_rate,
            "left_samples": left_list,
            "right_samples": right_list,
        }
        
        response = self._call_rust("calculate_groove", params)
        return response.get("points", [])
    
    def resample_audio(
        self,
        audio: Union['np.ndarray', List[float]],
        from_rate: int,
        to_rate: int,
    ) -> Union['np.ndarray', List[float]]:
        """
        Resample audio using high-quality Lanczos-3 resampling.
        
        Args:
            audio: Audio samples (numpy array or list, float32)
            from_rate: Current sample rate
            to_rate: Target sample rate
            
        Returns:
            Resampled audio (same type as input)
        """
        if from_rate == to_rate:
            return audio.copy() if HAS_NUMPY and hasattr(audio, 'copy') else list(audio)
        
        is_numpy = HAS_NUMPY and hasattr(audio, 'tolist')
        
        if is_numpy:
            audio_list = audio.astype(np.float32).tolist()
        else:
            audio_list = list(audio)
        
        params = {
            "from_rate": from_rate,
            "to_rate": to_rate,
            "samples": audio_list,
        }
        
        response = self._call_rust("resample_audio", params)
        samples = response.get("samples", [])
        
        if is_numpy:
            return np.array(samples, dtype=np.float32)
        return samples
    
    def apply_riaa(
        self,
        audio: Union['np.ndarray', List[float]],
        sample_rate: int,
    ) -> Union['np.ndarray', List[float]]:
        """
        Apply RIAA pre-emphasis equalization to audio.
        
        Args:
            audio: Audio samples (numpy array or list, float32)
            sample_rate: Sample rate in Hz
            
        Returns:
            RIAA-equalized audio (same type as input)
        """
        is_numpy = HAS_NUMPY and hasattr(audio, 'tolist')
        
        if is_numpy:
            audio_list = audio.astype(np.float32).tolist()
        else:
            audio_list = list(audio)
        
        params = {
            "sample_rate": sample_rate,
            "samples": audio_list,
        }
        
        response = self._call_rust("apply_riaa", params)
        samples = response.get("samples", [])
        
        if is_numpy:
            return np.array(samples, dtype=np.float32)
        return samples

    def run(
        self,
        audio_path: str,
        size_inch: int,
        rpm: int,
        groove_mode: str = "mono",
        sides: int = 1,
        output_prefix: str = "output",
        output_mode: str = "single",
        quality: str = "high",
        apply_riaa: bool = True,
        split_mode: str = "duplicate",
        groove_spacing_factor: float = 1.0,
        multi_files_mode: str = "auto",
        audio_paths: Optional[List[str]] = None,
        audio_paths_a: Optional[List[str]] = None,
        audio_paths_b: Optional[List[str]] = None,
        silence_between: float = 0.0,
        silence_between_a: float = 0.0,
        silence_between_b: float = 0.0,
    ) -> Dict[str, Any]:
        """Run the full Rust vinyl record pipeline and return the result."""
        params = {
            "audio_path": audio_path,
            "size_inch": size_inch,
            "rpm": rpm,
            "groove_mode": groove_mode,
            "sides": sides,
            "output_prefix": output_prefix,
            "output_mode": output_mode,
            "quality": quality,
            "apply_riaa": apply_riaa,
            "split_mode": split_mode,
            "groove_spacing_factor": groove_spacing_factor,
            "multi_files_mode": multi_files_mode,
            "silence_between": silence_between,
            "silence_between_a": silence_between_a,
            "silence_between_b": silence_between_b,
        }

        if audio_paths is not None:
            params["audio_paths"] = audio_paths
        if audio_paths_a is not None:
            params["audio_paths_a"] = audio_paths_a
        if audio_paths_b is not None:
            params["audio_paths_b"] = audio_paths_b

        return self._call_rust("run", params)


# Singleton instance for module-level convenience
_bridge = None


def get_bridge() -> RustBridge:
    """Get or create the Rust bridge singleton."""
    global _bridge
    if _bridge is None:
        _bridge = RustBridge()
    return _bridge


def calculate_groove(
    left: np.ndarray,
    right: np.ndarray,
    size_inch: int,
    rpm: int,
    groove_mode: str = "stereo",
    quality: str = "high",
    sample_rate: int = 44100,
) -> List[Dict[str, float]]:
    """Module-level convenience function for groove calculation."""
    return get_bridge().calculate_groove(
        left, right, size_inch, rpm, groove_mode, quality, sample_rate
    )


def resample_audio(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Module-level convenience function for audio resampling."""
    return get_bridge().resample_audio(audio, from_rate, to_rate)


def apply_riaa(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """Module-level convenience function for RIAA equalization."""
    return get_bridge().apply_riaa(audio, sample_rate)
