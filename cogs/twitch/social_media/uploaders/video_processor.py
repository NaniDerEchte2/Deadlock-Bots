"""
Video Processor - FFmpeg Wrapper für Video-Konvertierung.

Konvertiert Videos zu platform-spezifischen Formaten (9:16 für Shorts/Reels).
"""
import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple

log = logging.getLogger("TwitchStreams.VideoProcessor")


class VideoProcessor:
    """FFmpeg wrapper für Video-Konvertierung."""

    def __init__(self, ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe"):
        """
        Args:
            ffmpeg_path: Pfad zu ffmpeg binary (default: "ffmpeg" in PATH)
            ffprobe_path: Pfad zu ffprobe binary (default: "ffprobe" in PATH)
        """
        self.ffmpeg = ffmpeg_path
        self.ffprobe = ffprobe_path

    async def get_video_info(self, video_path: str) -> Dict:
        """
        Get video metadata (duration, resolution, aspect ratio).

        Args:
            video_path: Pfad zur Video-Datei

        Returns:
            Dict mit width, height, duration, aspect_ratio
        """
        try:
            cmd = [
                self.ffprobe,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration,r_frame_rate",
                "-of", "json",
                video_path,
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                raise Exception(f"ffprobe failed: {stderr.decode()}")

            data = json.loads(stdout.decode())
            stream = data["streams"][0]

            width = int(stream["width"])
            height = int(stream["height"])
            duration = float(stream.get("duration", 0))

            return {
                "width": width,
                "height": height,
                "duration": duration,
                "aspect_ratio": width / height if height > 0 else 0,
            }

        except FileNotFoundError:
            log.error("ffprobe not found in PATH")
            raise
        except Exception:
            log.exception("Failed to get video info for %s", video_path)
            raise

    async def convert_to_vertical(
        self,
        input_path: str,
        output_path: str,
        target_width: int = 1080,
        target_height: int = 1920,
        crop_mode: str = "center",
    ) -> bool:
        """
        Convert 16:9 (landscape) to 9:16 (portrait).

        Args:
            input_path: Input video path
            output_path: Output video path
            target_width: Target width (default: 1080)
            target_height: Target height (default: 1920)
            crop_mode: Crop mode ('center', 'top', 'bottom')

        Returns:
            True wenn erfolgreich
        """
        try:
            info = await self.get_video_info(input_path)

            log.info(
                "Converting video: %dx%d (%.2f:1) -> %dx%d (9:16)",
                info["width"],
                info["height"],
                info["aspect_ratio"],
                target_width,
                target_height,
            )

            # Build filter based on aspect ratio
            if info["aspect_ratio"] > 1:
                # Landscape video - crop to vertical
                crop_filter = self._build_crop_filter(
                    info["width"],
                    info["height"],
                    target_width,
                    target_height,
                    crop_mode,
                )
            else:
                # Already portrait or square - just scale
                crop_filter = f"scale={target_width}:{target_height}"

            cmd = [
                self.ffmpeg,
                "-i", input_path,
                "-vf", crop_filter,
                "-c:v", "libx264",
                "-preset", "medium",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                "-y",  # Overwrite output
                output_path,
            ]

            log.debug("FFmpeg command: %s", " ".join(cmd))

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                log.error("FFmpeg conversion failed: %s", stderr.decode())
                raise Exception(f"FFmpeg conversion failed: {stderr.decode()}")

            if not Path(output_path).exists():
                raise Exception(f"Output file not created: {output_path}")

            log.info("Video converted successfully: %s", output_path)
            return True

        except FileNotFoundError:
            log.error("ffmpeg not found in PATH")
            raise
        except Exception:
            log.exception("Failed to convert video")
            raise

    def _build_crop_filter(
        self,
        src_width: int,
        src_height: int,
        target_width: int,
        target_height: int,
        crop_mode: str,
    ) -> str:
        """
        Build FFmpeg crop filter.

        Args:
            src_width: Source width
            src_height: Source height
            target_width: Target width
            target_height: Target height
            crop_mode: 'center', 'top', or 'bottom'

        Returns:
            FFmpeg filter string
        """
        target_ratio = target_width / target_height
        src_ratio = src_width / src_height

        if src_ratio > target_ratio:
            # Source is wider - crop sides
            crop_w = int(src_height * target_ratio)
            crop_h = src_height
            crop_y = 0

            if crop_mode == "center":
                crop_x = (src_width - crop_w) // 2
            elif crop_mode == "left":
                crop_x = 0
            else:  # right
                crop_x = src_width - crop_w
        else:
            # Source is taller - crop top/bottom
            crop_w = src_width
            crop_h = int(src_width / target_ratio)

            if crop_mode == "center":
                crop_y = (src_height - crop_h) // 2
            elif crop_mode == "top":
                crop_y = 0
            else:  # bottom
                crop_y = src_height - crop_h

            crop_x = 0

        return f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale={target_width}:{target_height}"

    async def trim_video(
        self,
        input_path: str,
        output_path: str,
        max_duration: int = 60,
    ) -> bool:
        """
        Trim video to max duration.

        Args:
            input_path: Input video path
            output_path: Output video path
            max_duration: Max duration in seconds

        Returns:
            True wenn erfolgreich
        """
        try:
            info = await self.get_video_info(input_path)

            if info["duration"] <= max_duration:
                # No trimming needed - just copy
                import shutil
                shutil.copy2(input_path, output_path)
                log.debug("Video already under %s seconds - copied", max_duration)
                return True

            cmd = [
                self.ffmpeg,
                "-i", input_path,
                "-t", str(max_duration),
                "-c", "copy",
                "-y",
                output_path,
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            await proc.communicate()

            if proc.returncode != 0:
                raise Exception("FFmpeg trim failed")

            log.info("Video trimmed to %ss: %s", max_duration, output_path)
            return True

        except Exception:
            log.exception("Failed to trim video")
            raise

    async def convert_and_trim(
        self,
        input_path: str,
        output_path: str,
        max_duration: int = 60,
        target_width: int = 1080,
        target_height: int = 1920,
    ) -> bool:
        """
        All-in-one: Trim to max duration AND convert to vertical.

        Args:
            input_path: Input video path
            output_path: Output video path
            max_duration: Max duration in seconds
            target_width: Target width
            target_height: Target height

        Returns:
            True wenn erfolgreich
        """
        try:
            # First trim if needed
            info = await self.get_video_info(input_path)

            temp_path = input_path
            if info["duration"] > max_duration:
                temp_path = str(Path(output_path).with_suffix(".temp.mp4"))
                await self.trim_video(input_path, temp_path, max_duration)

            # Then convert to vertical
            await self.convert_to_vertical(
                temp_path,
                output_path,
                target_width,
                target_height,
            )

            # Clean up temp file
            if temp_path != input_path:
                Path(temp_path).unlink(missing_ok=True)

            return True

        except Exception:
            log.exception("Failed to convert and trim video")
            raise
