import os
from PIL import Image, ImageSequence, ImageOps
from typing import List, Tuple, Optional
from astrbot.api import logger  # 新增导入
import math

# 输入图片最长边上限：超限的输入图等比缩小，避免超大图导致处理过慢
_MAX_INPUT_SIZE = 1024


def _downscale_image(img: Image.Image, max_edge: int = _MAX_INPUT_SIZE) -> Image.Image:
    """等比缩小图片，使最长边不超过 max_edge，防止超大图处理过慢。

    若无需缩放则返回原图；若缩放则返回新图（由调用方负责 close 新图）。
    """
    w, h = img.size
    if max(w, h) > max_edge:
        scale = max_edge / max(w, h)
        nw = max(1, int(round(w * scale)))
        nh = max(1, int(round(h * scale)))
        return img.resize((nw, nh), Image.LANCZOS)
    return img


def _load_scaled_rgba(image_path: str, max_edge: int = _MAX_INPUT_SIZE) -> Image.Image:
    """打开图片 → 转为 RGBA → 按需等比缩小，返回可直接使用的 RGBA 图片。

    返回的图片对象统一由调用方负责 close（无论是否发生缩放）。
    超大图在入口处即被压缩，避免后续处理速度过慢。
    """
    raw = Image.open(image_path)
    img = raw
    try:
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
            raw.close()
        if max(img.size) > max_edge:
            scaled = _downscale_image(img, max_edge)
            img.close()
            img = scaled
        return img
    except Exception:
        img.close()
        raise


def is_apng(file_path: str) -> bool:
    """检测文件是否为 APNG 动图（PNG 格式且多帧）"""
    try:
        with Image.open(file_path) as img:
            if img.format != 'PNG':
                return False
            return getattr(img, 'n_frames', 1) > 1
    except Exception:
        return False

def is_gif(file_path: str) -> bool:
    """检测文件是否为 GIF 或 APNG 动图（用于决定是否走动图处理管线）

    注意：本插件将 APNG 动图也视为“GIF 类动图”，因为动图处理管线
    （process_gif_preserve → save_rgba_frames_as_gif）对 GIF 与 APNG
    一视同仁，均可逐帧处理并输出 GIF。这样 [表情:123] 等本地 APNG
    表情在处理后仍能保持动画，不会退化成静态图。
    """
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.gif':
        return True
    try:
        with open(file_path, 'rb') as f:
            header = f.read(6)
            if header.startswith(b'GIF87a') or header.startswith(b'GIF89a'):
                return True
    except Exception:
        pass
    return is_apng(file_path)

def is_animated_gif(gif_path: str) -> bool:
    """检测文件是否为动图（多帧 GIF 或 APNG）"""
    try:
        with Image.open(gif_path) as img:
            if img.format not in ('GIF', 'PNG'):
                return False
            frame_count = 0
            for _ in ImageSequence.Iterator(img):
                frame_count += 1
                if frame_count > 1:
                    return True
            return False
    except Exception as e:
        logger.error(f"检测动图失败 {gif_path}: {e}")
        return False

def process_gif_preserve(gif_path: str) -> Tuple[List[Image.Image],
                                                  List[int],
                                                  List[int],
                                                  int,
                                                  List[Optional[int]]]:
    """
    提取 GIF/APNG 的所有帧、时长、 disposal 方式、循环次数及透明度信息。

    - GIF：使用虚拟画布累积渲染，正确处理优化的 GIF（局部帧 + disposal）。
    - APNG：PIL 的 PNG 读取器已按 APNG 规范（blend/disposal）逐帧正确合成，
      直接取用即可。若再对 APNG 帧做「alpha 混合 + 画布累积」的二次合成，
      会因 APNG 的 blend=0（source 替换）语义把已消失的旧像素残留下来，
      产生残影（ghosting）。
    失败时抛出异常。
    """
    frames = []
    durations = []
    disposals = []
    transparencies = []
    loop = 0
    try:
        with Image.open(gif_path) as img:
            loop = img.info.get('loop', 0)
            # APNG 动图：PIL 已按规范合成好帧，直接取用，不做二次合成
            is_apng = (img.format == 'PNG' and getattr(img, 'is_animated', False))

            # 创建虚拟画布用于累积渲染（仅 GIF 需要）
            canvas = Image.new('RGBA', img.size, (0, 0, 0, 0))
            first_frame = True

            for frame in ImageSequence.Iterator(img):
                # 获取当前帧的 disposal 方式
                disposal = frame.info.get('disposal', 2)

                if is_apng:
                    # APNG：直接使用 PIL 已正确合成的帧
                    if frame.mode == 'RGBA':
                        current_frame = frame.copy()
                    else:
                        current_frame = frame.convert('RGBA')
                    frames.append(current_frame)
                else:
                    # 处理前一帧的 disposal（仅 GIF）
                    if not first_frame and disposals:
                        prev_disposal = disposals[-1]
                        if prev_disposal == 2:
                            # 恢复到背景（清除画布）
                            canvas = Image.new('RGBA', img.size, (0, 0, 0, 0))

                    # 将当前帧合成到画布上
                    if frame.mode == 'RGBA':
                        current_frame = frame.copy()
                    else:
                        current_frame = frame.convert('RGBA')

                    # 使用 alpha 通道作为掩码进行合成
                    alpha = current_frame.getchannel('A')
                    canvas.paste(current_frame, (0, 0), mask=alpha)
                    # 保存累积后的帧
                    frames.append(canvas.copy())

                durations.append(frame.info.get('duration', 100))
                disposals.append(disposal)
                transparencies.append(frame.info.get('transparency'))

                first_frame = False

            canvas.close()
            return frames, durations, disposals, loop, transparencies
    except Exception as e:
        logger.error(f"处理 GIF 提取失败 {gif_path}: {e}", exc_info=True)
        raise

def frames_to_rgba_preserve(frames: List[Image.Image]) -> List[Image.Image]:
    """将每一帧转换为 RGBA 模式，保留透明通道，失败时抛出异常"""
    rgba_frames = []
    try:
        for frame in frames:
            if frame.mode == 'RGBA':
                rgba_frames.append(frame.copy())
            elif frame.mode in ('LA', 'P') or 'transparency' in frame.info:
                rgba = frame.convert('RGBA')
                rgba_frames.append(rgba)
            else:
                # 对于 RGB 模式，添加完全不透明 alpha 通道
                rgba = frame.convert('RGBA')
                rgba_frames.append(rgba)
        return rgba_frames
    except Exception as e:
        logger.error(f"帧转 RGBA 失败: {e}", exc_info=True)
        for f in rgba_frames:
            f.close()
        raise

def save_rgba_frames_as_gif(frames: List[Image.Image],
                              durations: List[int],
                              loop: int,
                              output_path: str,
                              transparencies: List[Optional[int]] = None,
                              max_size: int = 500,
                              optimize_for_web: bool = True) -> None:
    """将 RGBA 帧列表保存为带透明度的 GIF，优化网络兼容性"""
    if not frames:
        raise ValueError("帧列表为空，无法保存")
    try:
        w, h = frames[0].size
        if w > max_size or h > max_size:
            scale = min(max_size / w, max_size / h)
            new_w = int(w * scale)
            new_h = int(h * scale)
            scaled_frames = []
            for frame in frames:
                scaled = frame.resize((new_w, new_h), Image.LANCZOS)
                scaled_frames.append(scaled)
                frame.close()
            frames = scaled_frames
            w, h = new_w, new_h

        # 保持 RGBA 模式以保留透明度
        rgba_frames = []
        for frame in frames:
            if frame.mode == 'RGBA':
                rgba_frames.append(frame.copy())
            elif frame.mode in ('LA', 'P'):
                rgba_frames.append(frame.convert('RGBA'))
            else:
                # RGB 模式转换为 RGBA（完全不透明）
                rgba = frame.convert('RGBA')
                rgba_frames.append(rgba)
            frame.close()

        # 直接保存 RGBA GIF（PIL 在最新版本支持）
        try:
            rgba_frames[0].save(
                output_path,
                save_all=True,
                append_images=rgba_frames[1:],
                duration=durations,
                loop=loop,
                disposal=2,
                optimize=optimize_for_web
            )
        except Exception as e:
            # 回退：转换为调色板模式，保留透明色
            logger.warning(f"RGBA GIF 保存失败，使用调色板模式: {e}")
            palette_frames = []
            for frame in rgba_frames:
                palette = frame.convert('P', palette=Image.Palette.ADAPTIVE, colors=256)
                palette_frames.append(palette)
            
            transparency = None
            if rgba_frames[0].mode == 'RGBA':
                alpha = rgba_frames[0].split()[3]
                if alpha.getextrema()[0] < 255:
                    transparency = 0
            
            palette_frames[0].save(
                output_path,
                save_all=True,
                append_images=palette_frames[1:],
                duration=durations,
                loop=loop,
                transparency=transparency,
                disposal=2,
                optimize=optimize_for_web
            )
            
            for frame in palette_frames:
                frame.close()

        for frame in rgba_frames:
            frame.close()
    except Exception as e:
        logger.error(f"保存 GIF 失败 {output_path}: {e}", exc_info=True)
        raise

def adjust_gif_speed(gif_path: str, speed_factor: float, output_path: str) -> None:
    """调整 GIF 播放速度，保留所有帧，失败抛出异常"""
    if not is_gif(gif_path):
        raise ValueError("文件不是有效的 GIF")
    frames, durations, disposals, loop, transparencies = process_gif_preserve(gif_path)
    if len(frames) <= 1:
        raise ValueError("GIF 至少需要两帧才能调整速度")
    try:
        rgba_frames = frames_to_rgba_preserve(frames)
        for f in frames:
            f.close()
        
        if speed_factor >= 1:
            # 加速：采样帧，但采用平均采样而不是简单的::step
            # 比如加速2倍时，100帧采样到50帧；加速4倍时，采样到25帧
            target_frame_count = max(2, int(len(rgba_frames) / speed_factor))
            indices = [int(i * len(rgba_frames) / target_frame_count) for i in range(target_frame_count)]
            selected_frames = [rgba_frames[i] for i in indices]
            selected_durations = [durations[i] if i < len(durations) else 100 for i in indices]
            save_rgba_frames_as_gif(selected_frames, selected_durations, loop, output_path, transparencies)
        else:
            # 慢放：复制帧，同时延长每帧的显示时间
            repeat = max(1, int(round(1 / speed_factor)))
            expanded_frames = []
            expanded_durations = []
            for i, frame in enumerate(rgba_frames):
                for _ in range(repeat):
                    expanded_frames.append(frame.copy())
                    expanded_durations.append(int(durations[i] * repeat) if i < len(durations) else 100)
            save_rgba_frames_as_gif(expanded_frames, expanded_durations, loop, output_path, transparencies)
    except Exception as e:
        for f in frames:
            f.close()
        raise

def reverse_gif(gif_path: str, output_path: str) -> None:
    """GIF 倒放，保留透明背景，失败抛出异常"""
    if not is_gif(gif_path):
        raise ValueError("文件不是有效的 GIF")
    frames, durations, disposals, loop, transparencies = process_gif_preserve(gif_path)
    if len(frames) <= 1:
        raise ValueError("GIF 至少需要两帧才能倒放")
    try:
        rgba_frames = frames_to_rgba_preserve(frames)
        for f in frames:
            f.close()
        rgba_frames.reverse()
        durations.reverse()
        transparencies.reverse()
        save_rgba_frames_as_gif(rgba_frames, durations, loop, output_path, transparencies)
    except Exception as e:
        for f in frames:
            f.close()
        raise

def apply_symmetry_to_static(image_path: str, output_path: str, direction: str) -> None:
    """对静态图片应用对称效果，失败抛出异常"""
    try:
        with Image.open(image_path) as img:
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGBA')
            else:
                img = img.convert('RGB')
            new_img = _apply_symmetry(img, direction)
            new_img.save(output_path, format='PNG')
            new_img.close()
    except Exception as e:
        logger.error(f"静态图对称处理失败 {image_path}: {e}", exc_info=True)
        raise

def apply_symmetry_to_gif(gif_path: str, output_path: str, direction: str) -> None:
    """对 GIF 动图应用对称效果，保留透明背景，失败抛出异常"""
    if not is_gif(gif_path):
        raise ValueError("文件不是有效的 GIF")
    frames, durations, disposals, loop, transparencies = process_gif_preserve(gif_path)
    try:
        rgba_frames = frames_to_rgba_preserve(frames)
        for f in frames:
            f.close()
        new_frames = [_apply_symmetry(frame, direction) for frame in rgba_frames]
        for f in rgba_frames:
            f.close()
        save_rgba_frames_as_gif(new_frames, durations, loop, output_path, transparencies)
    except Exception as e:
        for f in frames:
            f.close()
        for f in rgba_frames if 'rgba_frames' in locals() else []:
            f.close()
        raise

def _apply_symmetry(img: Image.Image, direction: str) -> Image.Image:
    """核心对称算法，失败时抛出异常"""
    try:
        w, h = img.size
        if direction in ('left', 'right'):
            left_w = w // 2
            right_w = w // 2
            center_w = w - left_w - right_w
            if direction == 'left':
                left_box = (0, 0, left_w, h)
                center_box = (left_w, 0, left_w + center_w, h) if center_w > 0 else None
                left_part = img.crop(left_box)
                left_mirror = left_part.transpose(Image.FLIP_LEFT_RIGHT)
                new_img = Image.new(img.mode, (w, h))
                new_img.paste(left_part, (0, 0))
                left_part.close()
                if center_box:
                    center_part = img.crop(center_box)
                    new_img.paste(center_part, (left_w, 0))
                    center_part.close()
                new_img.paste(left_mirror, (left_w + center_w, 0))
                left_mirror.close()
            else:  # 'right'
                right_box = (left_w + center_w, 0, w, h)
                center_box = (left_w, 0, left_w + center_w, h) if center_w > 0 else None
                right_part = img.crop(right_box)
                right_mirror = right_part.transpose(Image.FLIP_LEFT_RIGHT)
                new_img = Image.new(img.mode, (w, h))
                if center_box:
                    center_part = img.crop(center_box)
                    new_img.paste(center_part, (left_w, 0))
                    center_part.close()
                new_img.paste(right_mirror, (0, 0))
                new_img.paste(right_part, (left_w + center_w, 0))
                right_part.close()
                right_mirror.close()
        elif direction in ('top', 'bottom'):
            top_h = h // 2
            bottom_h = h // 2
            center_h = h - top_h - bottom_h
            if direction == 'top':
                top_box = (0, 0, w, top_h)
                center_box = (0, top_h, w, top_h + center_h) if center_h > 0 else None
                top_part = img.crop(top_box)
                top_mirror = top_part.transpose(Image.FLIP_TOP_BOTTOM)
                new_img = Image.new(img.mode, (w, h))
                new_img.paste(top_part, (0, 0))
                top_part.close()
                if center_box:
                    center_part = img.crop(center_box)
                    new_img.paste(center_part, (0, top_h))
                    center_part.close()
                new_img.paste(top_mirror, (0, top_h + center_h))
                top_mirror.close()
            else:  # 'bottom'
                bottom_box = (0, top_h + center_h, w, h)
                center_box = (0, top_h, w, top_h + center_h) if center_h > 0 else None
                bottom_part = img.crop(bottom_box)
                bottom_mirror = bottom_part.transpose(Image.FLIP_TOP_BOTTOM)
                new_img = Image.new(img.mode, (w, h))
                if center_box:
                    center_part = img.crop(center_box)
                    new_img.paste(center_part, (0, top_h))
                    center_part.close()
                new_img.paste(bottom_mirror, (0, 0))
                new_img.paste(bottom_part, (0, top_h + center_h))
                bottom_part.close()
                bottom_mirror.close()
        else:
            new_img = img.copy()
        return new_img
    except Exception as e:
        logger.error(f"对称处理失败: {e}", exc_info=True)
        raise

def invert_image(image_path: str, output_path: str) -> None:
    """对静态图片应用反色效果，失败抛出异常"""
    try:
        with Image.open(image_path) as img:
            if img.mode in ('RGBA', 'LA', 'P'):
                img_rgba = img.convert('RGBA')
                r, g, b, a = img_rgba.split()
                r_inv = ImageOps.invert(r)
                g_inv = ImageOps.invert(g)
                b_inv = ImageOps.invert(b)
                inverted = Image.merge('RGBA', (r_inv, g_inv, b_inv, a))
                inverted.save(output_path, format='PNG')
                inverted.close()
            else:
                img = img.convert('RGB')
                inverted = ImageOps.invert(img)
                inverted.save(output_path, format='PNG')
                inverted.close()
    except Exception as e:
        logger.error(f"静态图反色处理失败 {image_path}: {e}", exc_info=True)
        raise

def invert_gif(gif_path: str, output_path: str) -> None:
    """对 GIF 动图应用反色效果，保留透明背景，失败抛出异常"""
    if not is_gif(gif_path):
        raise ValueError("文件不是有效的 GIF")
    frames, durations, disposals, loop, transparencies = process_gif_preserve(gif_path)
    try:
        rgba_frames = frames_to_rgba_preserve(frames)
        for f in frames:
            f.close()
        inverted_frames = []
        for frame in rgba_frames:
            r, g, b, a = frame.split()
            r_inv = ImageOps.invert(r)
            g_inv = ImageOps.invert(g)
            b_inv = ImageOps.invert(b)
            inverted = Image.merge('RGBA', (r_inv, g_inv, b_inv, a))
            inverted_frames.append(inverted)
        for f in rgba_frames:
            f.close()
        save_rgba_frames_as_gif(inverted_frames, durations, loop, output_path, transparencies)
    except Exception as e:
        for f in frames:
            f.close()
        if 'rgba_frames' in locals():
            for f in rgba_frames:
                f.close()
        if 'inverted_frames' in locals():
            for f in inverted_frames:
                f.close()
        logger.error(f"GIF反色处理失败 {gif_path}: {e}", exc_info=True)
        raise

def rotate_image(image_path: str, output_path: str, angle: float) -> None:
    """旋转图片：90°/180°/270° 直接 expand；其他角度旋转后自动裁切至内容边缘"""
    try:
        with Image.open(image_path) as img:
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGBA')
            elif img.mode != 'RGBA':
                img = img.convert('RGB')
            
            if angle % 90 == 0:
                # 直角旋转：expand 即可完美贴合
                rotation = angle % 360
                if rotation == 0:
                    img.save(output_path, format='PNG' if img.mode == 'RGBA' else 'JPEG')
                else:
                    rotated = img.rotate(-angle, expand=True, fillcolor=(0, 0, 0, 0) if img.mode == 'RGBA' else (255, 255, 255))
                    if img.mode == 'RGBA':
                        rotated.save(output_path, format='PNG')
                    else:
                        rotated.save(output_path, format='JPEG')
                    rotated.close()
                return
            
            # 非直角旋转：先旋转到透明/白色画布，再裁切至内容边缘
            if img.mode == 'RGBA':
                rotated = img.rotate(-angle, expand=True, fillcolor=(0, 0, 0, 0))
                # 找到非透明像素的边界框
                alpha = rotated.split()[-1]
                bbox = alpha.getbbox()
            else:
                rotated = img.rotate(-angle, expand=True, fillcolor=(255, 255, 255))
                # 找到非白色像素的边界框（允许近白色容差）
                gray = rotated.convert('L')
                bbox = gray.point(lambda p: 0 if p > 250 else 255).getbbox()
            
            if bbox:
                cropped = rotated.crop(bbox)
                if cropped.mode == 'RGBA':
                    cropped.save(output_path, format='PNG')
                else:
                    cropped.save(output_path, format='JPEG')
                cropped.close()
            else:
                rotated.save(output_path, format='PNG' if rotated.mode == 'RGBA' else 'JPEG')
            
            rotated.close()
    except Exception as e:
        logger.error(f"静态图旋转处理失败 {image_path}: {e}", exc_info=True)
        raise

def rotate_gif(gif_path: str, output_path: str, angle: float) -> None:
    """GIF 旋转：90°/180°/270° 直接 expand；其他角度旋转后自动裁切至内容边缘"""
    if not is_gif(gif_path):
        raise ValueError("文件不是有效的GIF")
    
    frames, durations, disposals, loop, transparencies = process_gif_preserve(gif_path)
    
    try:
        rgba_frames = frames_to_rgba_preserve(frames)
        for f in frames:
            f.close()
        
        if angle % 90 == 0:
            # 直角旋转：expand 即可
            rotation = int(angle % 360)
            new_frames = []
            for frame in rgba_frames:
                if rotation == 0:
                    new_frames.append(frame.copy())
                else:
                    rotated = frame.rotate(-angle, expand=True, fillcolor=(0, 0, 0, 0))
                    new_frames.append(rotated)
            for f in rgba_frames:
                f.close()
            save_rgba_frames_as_gif(new_frames, durations, loop, output_path, transparencies)
            return
        
        # 非直角旋转：每帧旋转后裁切到内容边界
        rotated_frames = []
        union_bbox = None
        for frame in rgba_frames:
            rotated = frame.rotate(-angle, expand=True, fillcolor=(0, 0, 0, 0))
            alpha = rotated.split()[-1]
            bbox = alpha.getbbox()
            if bbox:
                if union_bbox is None:
                    union_bbox = list(bbox)
                else:
                    union_bbox[0] = min(union_bbox[0], bbox[0])
                    union_bbox[1] = min(union_bbox[1], bbox[1])
                    union_bbox[2] = max(union_bbox[2], bbox[2])
                    union_bbox[3] = max(union_bbox[3], bbox[3])
            rotated_frames.append(rotated)
        
        for f in rgba_frames:
            f.close()
        
        # 用统一边界裁切所有帧
        if union_bbox:
            cropped_frames = []
            for rotated in rotated_frames:
                cropped = rotated.crop(tuple(union_bbox))
                cropped_frames.append(cropped)
                rotated.close()
            save_rgba_frames_as_gif(cropped_frames, durations, loop, output_path, transparencies)
        else:
            save_rgba_frames_as_gif(rotated_frames, durations, loop, output_path, transparencies)
    except Exception as e:
        for f in frames:
            f.close()
        if 'rgba_frames' in locals():
            for f in rgba_frames:
                f.close()
        if 'rotated_frames' in locals():
            for f in rotated_frames:
                f.close()
        logger.error(f"GIF旋转处理失败 {gif_path}: {e}", exc_info=True)
        raise

def mirror_image(image_path: str, output_path: str) -> None:
    """对静态图片进行水平镜像翻转"""
    try:
        with Image.open(image_path) as img:
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGBA')
            else:
                img = img.convert('RGB')
            
            mirrored = img.transpose(Image.FLIP_LEFT_RIGHT)
            mirrored.save(output_path, format='PNG' if img.mode == 'RGBA' else 'JPEG')
            mirrored.close()
    except Exception as e:
        logger.error(f"静态图镜像处理失败 {image_path}: {e}", exc_info=True)
        raise

def mirror_gif(gif_path: str, output_path: str) -> None:
    """对GIF动图进行水平镜像翻转"""
    if not is_gif(gif_path):
        raise ValueError("文件不是有效的GIF")
    
    frames, durations, disposals, loop, transparencies = process_gif_preserve(gif_path)
    
    try:
        rgba_frames = frames_to_rgba_preserve(frames)
        for f in frames:
            f.close()
        
        mirrored_frames = []
        for frame in rgba_frames:
            mirrored = frame.transpose(Image.FLIP_LEFT_RIGHT)
            mirrored_frames.append(mirrored)
        
        for f in rgba_frames:
            f.close()
        
        save_rgba_frames_as_gif(mirrored_frames, durations, loop, output_path, transparencies)
    except Exception as e:
        for f in frames:
            f.close()
        if 'rgba_frames' in locals():
            for f in rgba_frames:
                f.close()
        if 'mirrored_frames' in locals():
            for f in mirrored_frames:
                f.close()
        logger.error(f"GIF镜像处理失败 {gif_path}: {e}", exc_info=True)
        raise

def shuffle_image(image_path: str, output_path: str) -> None:
    """对静态图片进行左右平移处理，从左往右正常，从右往左镜像"""
    try:
        with Image.open(image_path) as img:
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGBA')
            else:
                img = img.convert('RGB')
            
            w, h = img.size
            
            # 画布宽度：被引用图像的 4-5 倍，选择 5 倍以保证视觉效果
            canvas_w = int(w * 5)
            # 画布高度：被引用图像的 2-3 倍，选择 3 倍以保证垂直运动空间
            canvas_h = int(h * 3)
            
            # 总帧数：30 帧（15 去 + 15 回），X 方向匀速，Y 方向简谐振动
            total_frames = 30
            half_frames = total_frames // 2
            
            frames = []
            durations = []
            
            if img.mode != 'RGBA':
                img_rgba = img.convert('RGBA')
            else:
                img_rgba = img
            
            start_x = -w
            end_x = canvas_w
            
            # 垂直简谐振动参数
            amplitude = max(1, int(h * 0.18))
            center_y = (canvas_h - h) // 2
            
            # 前半段：从左往右；后半段：从右往左（均为匀速）
            for i in range(total_frames):
                if i < half_frames:
                    progress = i / (half_frames - 1) if half_frames > 1 else 0.0
                else:
                    j = i - half_frames
                    progress = 1.0 - (j / (half_frames - 1) if half_frames > 1 else 0.0)
                
                x_offset = int(start_x + (end_x - start_x) * progress)
                
                # 使用总帧进度产生简谐振动（一个完整正弦周期）
                total_progress = i / (total_frames - 1) if total_frames > 1 else 0.0
                y_offset = center_y + int(math.sin(total_progress * 2 * math.pi) * amplitude)
                
                canvas = Image.new('RGBA', (canvas_w, canvas_h), (0, 0, 0, 0))
                canvas.paste(img_rgba, (x_offset, y_offset), mask=img_rgba.split()[3] if img_rgba.mode == 'RGBA' else None)
                frames.append(canvas)
                durations.append(50)
            
            img_rgba.close()
            img.close()
            
            save_rgba_frames_as_gif(frames, durations, 0, output_path, max_size=500, optimize_for_web=True)
            
    except Exception as e:
        logger.error(f"静态图左右平移处理失败 {image_path}: {e}", exc_info=True)
        raise

def shuffle_gif(gif_path: str, output_path: str) -> None:
    """对GIF动图进行左右平移处理，优化性能和兼容性"""
    if not is_gif(gif_path):
        raise ValueError("文件不是有效的GIF")
    
    frames, durations, disposals, loop, transparencies = process_gif_preserve(gif_path)
    
    try:
        rgba_frames = frames_to_rgba_preserve(frames)
        for f in frames:
            f.close()
        
        if not rgba_frames:
            raise ValueError("没有有效的帧数据")
        
        w, h = rgba_frames[0].size
        
        # 画布尺寸：满足 用户要求的 4-5 倍宽度和 2-3 倍高度 — 取 5 倍宽、3 倍高
        canvas_w = int(w * 5)
        canvas_h = int(h * 3)
        
        # 总移动帧数，和静态图保持一致（30 帧：15 去 + 15 回）
        total_frames = 30
        half_frames = total_frames // 2
        
        new_frames = []
        new_durations = []
        
        start_x = -w
        end_x = canvas_w
        
        # 垂直简谐振动参数
        amplitude = max(1, int(h * 0.18))
        
        for i in range(total_frames):
            # 选择一个源帧（循环使用原始帧以保持动感）
            src = rgba_frames[i % len(rgba_frames)]
            if src.mode != 'RGBA':
                frame = src.convert('RGBA')
            else:
                frame = src.copy()
            
            if i < half_frames:
                progress = i / (half_frames - 1) if half_frames > 1 else 0.0
            else:
                j = i - half_frames
                progress = 1.0 - (j / (half_frames - 1) if half_frames > 1 else 0.0)
            
            x_offset = int(start_x + (end_x - start_x) * progress)
            
            total_progress = i / (total_frames - 1) if total_frames > 1 else 0.0
            y_offset = (canvas_h - h) // 2 + int(math.sin(total_progress * 2 * math.pi) * amplitude)
            
            canvas = Image.new('RGBA', (canvas_w, canvas_h), (0, 0, 0, 0))
            mask = frame.split()[3]
            canvas.paste(frame, (x_offset, y_offset), mask=mask)
            
            new_frames.append(canvas)
            new_durations.append(50)
            
            frame.close()
        
        # 清理原始帧
        for f in rgba_frames:
            f.close()
        
        # 保存结果
        save_rgba_frames_as_gif(new_frames, new_durations, loop, output_path, transparencies, max_size=500, optimize_for_web=True)
        
        # 清理新创建的帧
        for f in new_frames:
            f.close()
            
    except Exception as e:
        for f in frames:
            f.close()
        if 'rgba_frames' in locals():
            for f in rgba_frames:
                f.close()
        if 'new_frames' in locals():
            for f in new_frames:
                f.close()
        logger.error(f"GIF左右平移处理失败 {gif_path}: {e}", exc_info=True)
        raise

def bounce_image(image_path: str, output_path: str) -> None:
    """对静态图片进行蛙跳处理（向上跳跃，然后下落）"""
    try:
        with Image.open(image_path) as img:
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGBA')
            else:
                img = img.convert('RGB')
            
            w, h = img.size
            
            # 画布尺寸：给Y方向预留更多空间用于跳跃
            padding_x = int(w * 0.15)  # X方向只需要轻微空间
            padding_y = int(h * 0.5)   # Y方向预留更多空间用于跳跃
            canvas_w = w + padding_x * 2
            canvas_h = h + padding_y * 2
            
            # 跳跃参数
            num_frames = 20
            jump_height = padding_y      # 跳跃高度
            sway_distance = padding_x    # 轻微摇晃
            
            frames = []
            durations = []
            
            if img.mode != 'RGBA':
                img_rgba = img.convert('RGBA')
            else:
                img_rgba = img
            
            import math
            center_x = padding_x
            center_y = padding_y
            
            for i in range(num_frames):
                # 使用正弦函数模拟抛物线跳跃（0到pi，从起点到起点）
                progress = i / (num_frames - 1)
                angle = progress * math.pi  # 0 -> π，使用sin(angle)产生山峰
                
                # Y方向：抛物线运动（跳起再落下）
                # sin(angle) 在 0 到 π 时从 0 上升到 1 再回到 0
                y_offset = center_y - int(math.sin(angle) * jump_height)
                
                # X方向：轻微的左右摇晃（频率是Y的1.5倍，增加动感）
                sway_angle = progress * math.pi * 3
                x_offset = center_x + int(math.sin(sway_angle) * sway_distance * 0.6)
                
                canvas = Image.new('RGBA', (canvas_w, canvas_h), (0, 0, 0, 0))
                canvas.paste(img_rgba, (x_offset, y_offset), mask=img_rgba.split()[3] if img_rgba.mode == 'RGBA' else None)
                frames.append(canvas)
                
                # 跳跃过程中的速度变化：上升快，下落也快
                # 在最高点停留稍长一点
                if progress < 0.4:
                    durations.append(40)  # 上升快
                elif progress < 0.6:
                    durations.append(60)  # 最高点停留
                else:
                    durations.append(40)  # 下落快
            
            img_rgba.close()
            img.close()
            
            save_rgba_frames_as_gif(frames, durations, 0, output_path, max_size=500, optimize_for_web=True)
            
    except Exception as e:
        logger.error(f"静态图蛙跳处理失败 {image_path}: {e}", exc_info=True)
        raise

def bounce_gif(gif_path: str, output_path: str) -> None:
    """对GIF动图进行蛙跳处理（向上跳跃，然后下落）"""
    if not is_gif(gif_path):
        raise ValueError("文件不是有效的GIF")
    
    frames, durations, disposals, loop, transparencies = process_gif_preserve(gif_path)
    
    try:
        rgba_frames = frames_to_rgba_preserve(frames)
        for f in frames:
            f.close()
        
        if not rgba_frames:
            raise ValueError("没有有效的帧数据")
        
        w, h = rgba_frames[0].size
        
        # 画布尺寸：给Y方向预留更多空间用于跳跃
        padding_x = int(w * 0.15)  # X方向只需要轻微空间
        padding_y = int(h * 0.5)   # Y方向预留更多空间用于跳跃
        canvas_w = w + padding_x * 2
        canvas_h = h + padding_y * 2
        
        # 跳跃参数
        bounce_num_frames = 20
        jump_height = padding_y      # 跳跃高度
        sway_distance = padding_x    # 轻微摇晃
        
        # 生成新的帧序列
        new_frames = []
        new_durations = []
        
        import math
        center_x = padding_x
        center_y = padding_y
        
        # 为每一帧添加蛙跳效果
        for frame_idx, frame in enumerate(rgba_frames):
            # 确保帧是RGBA模式
            if frame.mode != 'RGBA':
                frame = frame.convert('RGBA')
            
            # 为这一帧生成多个跳跃变体
            for bounce_idx in range(bounce_num_frames):
                progress = bounce_idx / (bounce_num_frames - 1)
                angle = progress * math.pi  # 0 -> π
                
                # Y方向：抛物线运动（跳起再落下）
                y_offset = center_y - int(math.sin(angle) * jump_height)
                
                # X方向：轻微的左右摇晃（频率是Y的1.5倍）
                sway_angle = progress * math.pi * 3
                x_offset = center_x + int(math.sin(sway_angle) * sway_distance * 0.6)
                
                canvas = Image.new('RGBA', (canvas_w, canvas_h), (0, 0, 0, 0))
                mask = frame.split()[3]
                canvas.paste(frame, (x_offset, y_offset), mask=mask)
                
                new_frames.append(canvas)
                
                # 跳跃过程中的速度变化
                if progress < 0.4:
                    new_durations.append(40)  # 上升快
                elif progress < 0.6:
                    new_durations.append(60)  # 最高点停留
                else:
                    new_durations.append(40)  # 下落快
        
        # 清理原始帧
        for f in rgba_frames:
            f.close()
        
        # 保存结果
        save_rgba_frames_as_gif(new_frames, new_durations, loop, output_path, transparencies, max_size=500, optimize_for_web=True)
        
        # 清理新创建的帧
        for f in new_frames:
            f.close()
            
    except Exception as e:
        for f in frames:
            f.close()
        if 'rgba_frames' in locals():
            for f in rgba_frames:
                f.close()
        if 'new_frames' in locals():
            for f in new_frames:
                f.close()
        logger.error(f"GIF蛙跳处理失败 {gif_path}: {e}", exc_info=True)
        raise


def kaleidoscope_image(image_path: str, output_path: str, segments: int = 12, max_size: int = 500) -> None:
    """对静态图片应用万花镜效果：八方向×五层放射铺排，失败抛出异常

    将原图等比缩小后，围绕画布中心沿八个方向（上/右上/右/右下/下/左下/左/左上）
    各向外铺开 5 张（共 40 张）。图像由内到外逐层增大、允许部分重叠，
    外侧的大图覆盖内侧的小图。每张按所在方向对应的角度旋转
    （上 0°、右上 45°、右 90°、右下 135°、下 180°、左下 225°、左 270°、左上 315°），
    画布尺寸自动计算，其余区域用透明填充。

    Args:
        image_path: 输入图片路径
        output_path: 输出图片路径
        segments: 已废弃，保留以兼容旧接口（新效果固定为八方向×五层）
        max_size: 最内层（第1层）单张图最长边的参考上限（像素）
    """
    try:
        img = _load_scaled_rgba(image_path)
        try:
            _kaleidoscope_tile(img, output_path, max_size)
        finally:
            img.close()
    except Exception as e:
        logger.error(f"静态图万花镜处理失败 {image_path}: {e}", exc_info=True)
        raise


# ===== 万花镜（放射铺排版） =====
_KALEIDOSCOPE_LAYERS = 5  # 每个方向向外铺开的层数
# 八个方向：(dx, dy) 方向向量，deg 为对应旋转角度（上方 0°，顺时针递增）
_KALEIDOSCOPE_DIRECTIONS = [
    (0, -1, 0),      # 上
    (1, -1, 45),     # 右上
    (1, 0, 90),      # 右
    (1, 1, 135),     # 右下
    (0, 1, 180),     # 下
    (-1, 1, 225),    # 左下
    (-1, 0, 270),    # 左
    (-1, -1, 315),   # 左上
]


def _kaleidoscope_tile(src: Image.Image, output_path: str, max_size: int = 500) -> None:
    """八方向×五层放射铺排核心：由内到外逐层增大，允许重叠，外层覆盖内层"""
    w, h = src.size

    # 1. 由内到外逐层增大：第 k 层最长边 = base * (1 + (k-1)*growth)
    base_max = max(40, min(80, (max_size // 6) if max_size and max_size > 0 else 80))
    growth = 0.42  # 每层递增比例（第5层约为第1层的2.7倍）
    layer_sizes = []   # 每层 (tile_w, tile_h)
    layer_dists = []   # 每层中心到画布中心的距离（逐层累计）
    dist_acc = 0.0
    for k in range(1, _KALEIDOSCOPE_LAYERS + 1):
        tile_max = base_max * (1 + (k - 1) * growth)
        scale = min(tile_max / w, tile_max / h)
        tw = max(1, int(round(w * scale)))
        th = max(1, int(round(h * scale)))
        layer_sizes.append((tw, th))
        # 层间距：明显小于该层图的最长边 → 相邻层产生较多重叠
        step = max(tw, th) * 0.62
        dist_acc += step
        layer_dists.append(dist_acc)
    max_dist = layer_dists[-1]

    # 归一化方向向量
    dirs = [(dx / math.hypot(dx, dy), dy / math.hypot(dx, dy), deg)
            for dx, dy, deg in _KALEIDOSCOPE_DIRECTIONS]

    # 2. 自动计算画布尺寸：容纳最外层（最大）旋转后的图，其余区域透明
    max_diag_half = max(math.hypot(tw, th) / 2.0 for tw, th in layer_sizes)
    padding = 8
    radius = int(max_dist + max_diag_half) + padding
    canvas_size = 2 * radius
    canvas = Image.new('RGBA', (canvas_size, canvas_size), (0, 0, 0, 0))
    cx = canvas_size / 2.0
    cy = canvas_size / 2.0

    # 3. 由内到外逐层铺开（外层后贴，覆盖内层），每张按方向角度旋转
    for idx, (tw, th) in enumerate(layer_sizes):
        dist = layer_dists[idx]
        # 该层统一缩放一次
        if (tw, th) == (w, h):
            tile_k = src.copy()
        else:
            tile_k = src.resize((tw, th), Image.LANCZOS)
        for dx, dy, deg in dirs:
            # PIL rotate 正角为逆时针，取 -deg 实现“顺时针 deg”
            rotated = tile_k.rotate(-deg, expand=True, resample=Image.BICUBIC, fillcolor=(0, 0, 0, 0))
            rw, rh = rotated.size
            pos_x = int(round(cx + dx * dist - rw / 2.0))
            pos_y = int(round(cy + dy * dist - rh / 2.0))
            canvas.paste(rotated, (pos_x, pos_y), rotated)
            rotated.close()
        tile_k.close()

    canvas.save(output_path, format='PNG')
    canvas.close()


def kaleidoscope_gif(gif_path: str, output_path: str, segments: int = 12, max_size: int = 500) -> None:
    """对 GIF 动图应用万花镜效果，保留透明背景，失败抛出异常"""
    if not is_gif(gif_path):
        raise ValueError("文件不是有效的 GIF")
    frames, durations, disposals, loop, transparencies = process_gif_preserve(gif_path)
    try:
        rgba_frames = frames_to_rgba_preserve(frames)
        for f in frames:
            f.close()
        
        processed_frames = []
        import tempfile
        import os as _os
        
        for frame in rgba_frames:
            # Save frame to temp file and process
            tmp_in = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            tmp_out = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            try:
                tmp_in.close()
                tmp_out.close()
                scaled = _downscale_image(frame)
                try:
                    scaled.save(tmp_in.name, format='PNG')
                finally:
                    if scaled is not frame:
                        scaled.close()
                kaleidoscope_image(tmp_in.name, tmp_out.name, segments=segments, max_size=max_size)
                processed = Image.open(tmp_out.name)
                processed.load()
                processed_frames.append(processed.copy())
                processed.close()
            finally:
                for p in [tmp_in.name, tmp_out.name]:
                    if _os.path.exists(p):
                        _os.remove(p)
        
        for f in rgba_frames:
            f.close()
        
        # Build new duration list matching frame count
        new_durations = []
        for i in range(len(processed_frames)):
            new_durations.append(durations[i % len(durations)])
        
        save_rgba_frames_as_gif(processed_frames, new_durations, loop, output_path, transparencies)
    except Exception as e:
        for f in frames:
            f.close()
        if 'rgba_frames' in locals():
            for f in rgba_frames:
                f.close()
        if 'processed_frames' in locals():
            for f in processed_frames:
                f.close()
        logger.error(f"GIF万花镜处理失败 {gif_path}: {e}", exc_info=True)
        raise


# ===== 万花筒（圆形扇区镜像） =====
def tube_image(image_path: str, output_path: str, segments: int = 12, max_size: int = 500) -> None:
    """对静态图片应用万花筒效果（圆形扇区镜像），失败抛出异常

    将图片按中心正方形裁剪后，按 segments 份扇形镜像映射，形成对称的万花筒图案。

    Args:
        image_path: 输入图片路径
        output_path: 输出图片路径
        segments: 扇形份数（默认12份，即6组镜像，需为偶数）
        max_size: 输出最大尺寸（像素）
    """
    try:
        import numpy as np
        _tube_numpy(image_path, output_path, segments, max_size)
    except ImportError:
        logger.warning("numpy 不可用，使用纯 PIL 实现万花筒（较慢）")
        _tube_pure_pil(image_path, output_path, segments, max_size)


def _tube_numpy(image_path: str, output_path: str, segments: int, max_size: int) -> None:
    """使用 numpy 实现的万花筒效果（高性能）"""
    import numpy as np

    try:
        img = _load_scaled_rgba(image_path)
        try:
            w, h = img.size
            size = min(w, h)
            left = (w - size) // 2
            top = (h - size) // 2
            cropped = img.crop((left, top, left + size, top + size))

            if size > max_size:
                cropped = cropped.resize((max_size, max_size), Image.LANCZOS)
                size = max_size

            src = np.array(cropped, dtype=np.float32)
            cropped.close()
        finally:
            img.close()

        radius = size // 2
        cx = size / 2.0
        cy = size / 2.0
        angle = 2 * math.pi / segments  # e.g., 30° for 12 segments

        y_coords, x_coords = np.mgrid[:size, :size]
        dx = x_coords - cx
        dy = y_coords - cy
        r = np.sqrt(dx * dx + dy * dy)

        output = np.zeros((size, size, 4), dtype=np.uint8)

        inside = r <= radius
        r_inside = r[inside]
        dx_inside = dx[inside]
        dy_inside = dy[inside]

        theta = np.arctan2(dy_inside, dx_inside)
        theta[theta < 0] += 2 * np.pi

        seg_idx = np.floor(theta / angle).astype(np.int32)
        frac = theta / angle - seg_idx.astype(np.float32)

        odd_mask = (seg_idx % 2 == 1)
        frac[odd_mask] = 1.0 - frac[odd_mask]

        frac = np.clip(frac, 0.0, 1.0)

        source_theta = frac * angle
        sx = cx + r_inside * np.cos(source_theta)
        sy = cy + r_inside * np.sin(source_theta)

        sx_floor = np.floor(sx).astype(np.int32)
        sy_floor = np.floor(sy).astype(np.int32)
        fx = sx - sx_floor.astype(np.float32)
        fy = sy - sy_floor.astype(np.float32)

        sx_floor = np.clip(sx_floor, 0, size - 2)
        sy_floor = np.clip(sy_floor, 0, size - 2)
        sx_ceil = sx_floor + 1
        sy_ceil = sy_floor + 1

        tl = src[sy_floor, sx_floor]
        tr = src[sy_floor, sx_ceil]
        bl = src[sy_ceil, sx_floor]
        br = src[sy_ceil, sx_ceil]

        fx = fx[..., np.newaxis]
        fy = fy[..., np.newaxis]
        top = tl * (1 - fx) + tr * fx
        bot = bl * (1 - fx) + br * fx
        sampled = (top * (1 - fy) + bot * fy).astype(np.uint8)

        y_idx, x_idx = np.where(inside)
        output[y_idx, x_idx] = sampled

        result = Image.fromarray(output, 'RGBA')
        result.save(output_path, format='PNG')
        result.close()
    except Exception as e:
        logger.error(f"静态图万花筒处理失败 {image_path}: {e}", exc_info=True)
        raise


def _tube_pure_pil(image_path: str, output_path: str, segments: int, max_size: int) -> None:
    """使用纯 PIL 实现的万花筒效果（回退方案）"""
    img = _load_scaled_rgba(image_path)
    try:
        w, h = img.size
        size = min(w, h)
        left = (w - size) // 2
        top = (h - size) // 2
        img = img.crop((left, top, left + size, top + size))

        if size > max_size:
            img = img.resize((max_size, max_size), Image.LANCZOS)
            size = max_size

        src_pixels = img.load()

        radius = size // 2
        cx = size / 2.0
        cy = size / 2.0
        angle = 2 * math.pi / segments

        output = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        out_pixels = output.load()

        for y in range(size):
            for x in range(size):
                dx = x - cx
                dy = y - cy
                r = math.sqrt(dx * dx + dy * dy)
                if r > radius:
                    continue

                theta = math.atan2(dy, dx)
                if theta < 0:
                    theta += 2 * math.pi

                seg_idx = int(theta / angle)
                frac = theta / angle - seg_idx

                if seg_idx % 2 == 1:
                    frac = 1.0 - frac

                frac = max(0.0, min(1.0, frac))

                source_theta = frac * angle
                sx = cx + r * math.cos(source_theta)
                sy = cy + r * math.sin(source_theta)

                sx_int = int(sx)
                sy_int = int(sy)

                if 0 <= sx_int < size - 1 and 0 <= sy_int < size - 1:
                    fx = sx - sx_int
                    fy = sy - sy_int

                    tl = src_pixels[sx_int, sy_int]
                    tr = src_pixels[sx_int + 1, sy_int]
                    bl = src_pixels[sx_int, sy_int + 1]
                    br = src_pixels[sx_int + 1, sy_int + 1]

                    r_val = int((tl[0] * (1 - fx) + tr[0] * fx) * (1 - fy) +
                                (bl[0] * (1 - fx) + br[0] * fx) * fy)
                    g_val = int((tl[1] * (1 - fx) + tr[1] * fx) * (1 - fy) +
                                (bl[1] * (1 - fx) + br[1] * fx) * fy)
                    b_val = int((tl[2] * (1 - fx) + tr[2] * fx) * (1 - fy) +
                                (bl[2] * (1 - fx) + br[2] * fx) * fy)
                    a_val = int((tl[3] * (1 - fx) + tr[3] * fx) * (1 - fy) +
                                (bl[3] * (1 - fx) + br[3] * fx) * fy)

                    out_pixels[x, y] = (r_val, g_val, b_val, a_val)
                elif 0 <= sx_int < size and 0 <= sy_int < size:
                    out_pixels[x, y] = src_pixels[sx_int, sy_int]

        output.save(output_path, format='PNG')
        output.close()
    finally:
        img.close()


def tube_gif(gif_path: str, output_path: str, segments: int = 12, max_size: int = 500) -> None:
    """对 GIF 动图应用万花筒效果，保留透明背景，失败抛出异常"""
    if not is_gif(gif_path):
        raise ValueError("文件不是有效的 GIF")
    frames, durations, disposals, loop, transparencies = process_gif_preserve(gif_path)
    try:
        rgba_frames = frames_to_rgba_preserve(frames)
        for f in frames:
            f.close()

        processed_frames = []
        import tempfile
        import os as _os

        for frame in rgba_frames:
            # Save frame to temp file and process
            tmp_in = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            tmp_out = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
            try:
                tmp_in.close()
                tmp_out.close()
                scaled = _downscale_image(frame)
                try:
                    scaled.save(tmp_in.name, format='PNG')
                finally:
                    if scaled is not frame:
                        scaled.close()
                tube_image(tmp_in.name, tmp_out.name, segments=segments, max_size=max_size)
                processed = Image.open(tmp_out.name)
                processed.load()
                processed_frames.append(processed.copy())
                processed.close()
            finally:
                for p in [tmp_in.name, tmp_out.name]:
                    if _os.path.exists(p):
                        _os.remove(p)

        for f in rgba_frames:
            f.close()

        # Build new duration list matching frame count
        new_durations = []
        for i in range(len(processed_frames)):
            new_durations.append(durations[i % len(durations)])

        save_rgba_frames_as_gif(processed_frames, new_durations, loop, output_path, transparencies)
    except Exception as e:
        for f in frames:
            f.close()
        if 'rgba_frames' in locals():
            for f in rgba_frames:
                f.close()
        if 'processed_frames' in locals():
            for f in processed_frames:
                f.close()
        logger.error(f"GIF万花筒处理失败 {gif_path}: {e}", exc_info=True)
        raise
