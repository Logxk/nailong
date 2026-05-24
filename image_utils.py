import os
from PIL import Image, ImageSequence, ImageOps
from typing import List, Tuple, Optional
from astrbot.api import logger  # 新增导入
import math

def is_gif(file_path: str) -> bool:
    """检测文件是否为 GIF（扩展名或文件头）"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.gif':
        return True
    try:
        with open(file_path, 'rb') as f:
            header = f.read(6)
            return header.startswith(b'GIF87a') or header.startswith(b'GIF89a')
    except Exception:
        return False

def is_animated_gif(gif_path: str) -> bool:
    """检测 GIF 是否为动图（多于 1 帧）"""
    try:
        with Image.open(gif_path) as img:
            if img.format != 'GIF':
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
    提取 GIF 的所有帧、时长、 disposal 方式、循环次数及透明度信息。
    使用虚拟画布正确处理优化的 GIF（累积渲染）。
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

            # 创建虚拟画布用于累积渲染
            canvas = Image.new('RGBA', img.size, (0, 0, 0, 0))
            first_frame = True

            for frame in ImageSequence.Iterator(img):
                # 获取当前帧的 disposal 方式
                disposal = frame.info.get('disposal', 2)

                # 处理前一帧的 disposal
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
    """对静态图片旋转指定角度，非90°倍数时缩小并空白填充"""
    try:
        with Image.open(image_path) as img:
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGBA')
            elif img.mode != 'RGBA':
                img = img.convert('RGB')
            
            w, h = img.size
            
            if angle % 90 == 0:
                rotation = angle % 360
                if rotation == 0:
                    img.save(output_path, format='PNG' if img.mode == 'RGBA' else 'JPEG')
                else:
                    rotated = img.rotate(-angle, expand=True, fillcolor=(0, 0, 0, 0) if img.mode == 'RGBA' else (255, 255, 255))
                    if img.mode == 'RGBA':
                        rotated.save(output_path, format='PNG')
                    else:
                        rotated.save(output_path, format='JPEG')
                return
            
            angle_rad = math.radians(abs(angle))
            
            cos_val = abs(math.cos(angle_rad))
            sin_val = abs(math.sin(angle_rad))
            
            new_w = int(w * cos_val + h * sin_val)
            new_h = int(w * sin_val + h * cos_val)
            
            scale = min(new_w / w, new_h / h) * 0.95
            
            if scale < 1:
                new_w = int(w * scale)
                new_h = int(h * scale)
                img = img.resize((new_w, new_h), Image.LANCZOS)
            
            if img.mode == 'RGBA':
                canvas = Image.new('RGBA', (new_w, new_h), (0, 0, 0, 0))
            else:
                canvas = Image.new('RGB', (new_w, new_h), (255, 255, 255))
            
            paste_x = (new_w - img.width) // 2
            paste_y = (new_h - img.height) // 2
            
            if img.mode == 'RGBA':
                canvas.paste(img, (paste_x, paste_y), mask=img.split()[3])
            else:
                canvas.paste(img, (paste_x, paste_y))
            
            rotated = canvas.rotate(-angle, expand=True, fillcolor=(0, 0, 0, 0) if canvas.mode == 'RGBA' else (255, 255, 255))
            
            if rotated.mode == 'RGBA':
                rotated.save(output_path, format='PNG')
            else:
                rotated.save(output_path, format='JPEG')
            
            canvas.close()
            rotated.close()
    except Exception as e:
        logger.error(f"静态图旋转处理失败 {image_path}: {e}", exc_info=True)
        raise

def rotate_gif(gif_path: str, output_path: str, angle: float) -> None:
    """对GIF动图旋转指定角度，非90°倍数时缩小并空白填充"""
    if not is_gif(gif_path):
        raise ValueError("文件不是有效的GIF")
    
    frames, durations, disposals, loop, transparencies = process_gif_preserve(gif_path)
    
    try:
        rgba_frames = frames_to_rgba_preserve(frames)
        for f in frames:
            f.close()
        
        angle_rad = math.radians(abs(angle))
        
        if angle % 90 == 0:
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
        
        w, h = rgba_frames[0].size
        cos_val = abs(math.cos(angle_rad))
        sin_val = abs(math.sin(angle_rad))
        
        new_w = int(w * cos_val + h * sin_val)
        new_h = int(w * sin_val + h * cos_val)
        
        scale = min(new_w / w, new_h / h) * 0.95
        
        rotated_frames = []
        for frame in rgba_frames:
            if scale < 1:
                resized = frame.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            else:
                resized = frame.copy()
            
            canvas = Image.new('RGBA', (new_w, new_h), (0, 0, 0, 0))
            paste_x = (new_w - resized.width) // 2
            paste_y = (new_h - resized.height) // 2
            canvas.paste(resized, (paste_x, paste_y), mask=resized.split()[3])
            
            rotated = canvas.rotate(-angle, expand=True, fillcolor=(0, 0, 0, 0))
            rotated_frames.append(rotated)
            
            if scale < 1:
                resized.close()
            canvas.close()
        
        for f in rgba_frames:
            f.close()
        
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
