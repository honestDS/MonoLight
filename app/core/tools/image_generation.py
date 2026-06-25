import base64
import json
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.core.channel_router import select_channel
from app.core.exceptions import BaseBusinessException
from app.core.paths import get_user_temp_dir
from app.models.channel import ChannelConfig
from app.providers.image_generation import ImageGenerationClient

from .base import BaseExecutor
from .send_file_to_user import _encode_token

IMAGE_GENERATION_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": "Generate one image using the active profile's configured image generation model. Use it when the user asks to create, draw, render, or generate an image.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "A detailed image generation prompt describing the subject, scene, style, composition, colors, and constraints.",
                },
                "size": {
                    "type": "string",
                    "enum": ["1024x1024", "1024x1536", "1536x1024"],
                    "description": "Image size.",
                    "default": "1024x1024",
                },
                "quality": {
                    "type": "string",
                    "enum": ["auto", "low", "medium", "high"],
                    "description": "Image quality.",
                    "default": "auto",
                },
            },
            "required": ["prompt"],
        },
    },
}


class ImageGenerationExecutor(BaseExecutor):
    def _get_channel_config(self) -> ChannelConfig | None:
        channel_group = getattr(self.cfg, "channel", None)
        if not channel_group:
            return None
        return getattr(channel_group, "image_generation_channel", None)

    def _get_generated_image_dir(self) -> Path:
        image_dir = get_user_temp_dir(self.project_root, self.uid) / "generated_images"
        image_dir.mkdir(parents=True, exist_ok=True)
        return image_dir

    async def _save_base64_image(self, b64_json: str) -> dict[str, Any]:
        image_bytes = base64.b64decode(b64_json)
        file_name = f"generated_image_{uuid.uuid4().hex}.png"
        image_path = (self._get_generated_image_dir() / file_name).resolve()

        def write_image():
            image_path.write_bytes(image_bytes)

        await self.run_sync(write_image)
        token = _encode_token({"path": str(image_path), "uid": self.uid, "id": uuid.uuid4().hex})
        return {
            "id": token,
            "name": file_name,
            "description": "Generated image",
            "mime_type": "image/png",
            "size": len(image_bytes),
            "download_url": f"/api/v1/download-sent?token={quote(token)}",
            "previewable": True,
        }

    async def execute(
        self,
        prompt: str,
        size: str | None = None,
        quality: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not self.db or not self.profile or not self.cfg:
            return json.dumps({"status": "failed", "error": "Runtime context is missing."}, ensure_ascii=False)

        image_channel = self._get_channel_config()
        if not image_channel or not image_channel.rules:
            return json.dumps(
                {
                    "status": "failed",
                    "error": "No image generation channel is configured in the active profile.",
                },
                ensure_ascii=False,
            )

        prompt_text = (prompt or "").strip()
        if not prompt_text:
            return json.dumps({"status": "failed", "error": "prompt is required."}, ensure_ascii=False)

        excluded_priorities: set[int] = set()
        last_error = ""
        cursor_key = f"{self.profile.id}:IMAGE_GENERATION" if self.profile.id else None

        while True:
            selection = await select_channel(
                self.db,
                image_channel,
                "IMAGE_GENERATION",
                call_context="image_generation_tool",
                excluded_priorities=excluded_priorities,
                cursor_key=cursor_key,
            )
            if not selection:
                return json.dumps(
                    {
                        "status": "failed",
                        "error": last_error or "No available image generation channel.",
                    },
                    ensure_ascii=False,
                )

            channel, model_entry, rule = selection
            try:
                resolved_size = size or model_entry.get("size") or "1024x1024"
                resolved_quality = quality or model_entry.get("quality") or "auto"
                response = await ImageGenerationClient.generate_image(
                    channel_type=channel.channel_type,
                    api_key=channel.get_decrypted_api_key(),
                    base_url=channel.base_url or "",
                    model_id=model_entry["model_id"],
                    prompt=prompt_text,
                    size=resolved_size,
                    n=1,
                    quality=resolved_quality,
                    timeout=float(getattr(getattr(self.cfg, "tool", None), "image_generation_timeout", 60.0) or 60.0),
                )
                images = response.get("data") if isinstance(response, dict) else None
                if not isinstance(images, list) or not images:
                    return json.dumps(
                        {
                            "status": "failed",
                            "error": "The image generation model did not return an image.",
                            "model": model_entry["model_id"],
                        },
                        ensure_ascii=False,
                    )

                image = images[0] if isinstance(images[0], dict) else {}
                model_name = response.get("model", model_entry["model_id"]) if isinstance(response, dict) else model_entry["model_id"]

                if image.get("url"):
                    return json.dumps(
                        {
                            "status": "success",
                            "type": "generated_image",
                            "model": model_name,
                            "channel_id": channel.id,
                            "channel_name": channel.name,
                            "prompt": prompt_text,
                            "size": resolved_size,
                            "quality": resolved_quality,
                            "image": {
                                "url": image.get("url"),
                                "revised_prompt": image.get("revised_prompt"),
                            },
                        },
                        ensure_ascii=False,
                    )

                if image.get("b64_json"):
                    file_item = await self._save_base64_image(str(image["b64_json"]))
                    return json.dumps(
                        {
                            "status": "success",
                            "type": "files_to_user",
                            "files": [file_item],
                            "errors": [],
                            "model": model_name,
                            "channel_id": channel.id,
                            "channel_name": channel.name,
                            "prompt": prompt_text,
                            "size": resolved_size,
                            "quality": resolved_quality,
                            "message": "Image generated successfully. The generated image file will be automatically appended after the assistant reply in the chat UI.",
                        },
                        ensure_ascii=False,
                    )

                return json.dumps(
                    {
                        "status": "failed",
                        "error": "The image generation model returned an image item without url or b64_json.",
                        "model": model_name,
                    },
                    ensure_ascii=False,
                )
            except BaseBusinessException as exc:
                last_error = exc.message
            except Exception as exc:
                last_error = str(exc)

            excluded_priorities.add(rule.priority)
            if not image_channel.retry_on_failure:
                return json.dumps({"status": "failed", "error": last_error}, ensure_ascii=False)

