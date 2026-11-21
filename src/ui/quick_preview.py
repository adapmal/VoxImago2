"""Diálogo compartilhado de pré-visualização rápida (Barra de Espaço) para grade e comparação por hash."""

from __future__ import annotations

import os
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QSlider
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QPixmap, QImage
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PIL import Image

try:
    import rawpy
except ImportError:
    rawpy = None


def open_quick_preview_dialog(
    parent,
    items: list[dict],
    start_index: int = 0,
    resolve_path_fn=None,
    on_step_fn=None,
):
    """Abre pré-visualização ampliada com navegação por setas, Barra de Espaço e Esc."""
    if not items:
        return

    index_state = [max(0, min(start_index, len(items) - 1))]
    current_player = [None]
    dialog = QDialog(parent)
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(10, 10, 10, 10)
    layout.setSpacing(5)
    dialog.setLayout(layout)
    dialog.setModal(True)

    def _resolve(item_dict):
        if resolve_path_fn:
            p = resolve_path_fn(item_dict)
            if p:
                return p
        return item_dict.get('physical_path') or item_dict.get('path') if item_dict else None

    def load_item(idx: int):
        item_dict = items[idx]
        file_path = _resolve(item_dict)
        if not file_path or not os.path.exists(file_path):
            return False

        if current_player[0]:
            try:
                current_player[0].stop()
                current_player[0].setSource(QUrl())
                current_player[0] = None
            except Exception:
                pass

        def clear_layout(lay):
            while lay.count():
                child = lay.takeAt(0)
                if child.widget():
                    child.widget().deleteLater()
                elif child.layout():
                    clear_layout(child.layout())
                    child.layout().deleteLater()

        clear_layout(layout)

        role_prefix = f"{item_dict['preview_role']} — " if item_dict.get('preview_role') else ""
        counter_suffix = f" ({idx + 1}/{len(items)})" if len(items) > 1 else ""
        dialog.setWindowTitle(f"Pré-visualização{counter_suffix} - {role_prefix}{os.path.basename(file_path)}")

        ext = os.path.splitext(file_path)[1].lower()
        video_exts = {
            '.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm', '.m4v',
            '.3gp', '.mts', '.m2ts', '.ts', '.mpg', '.mpeg', '.vob',
        }
        raw_exts = {'.cr2', '.nef', '.arw', '.dng', '.orf', '.rw2', '.pef', '.srw', '.raf', '.raw'}
        heic_exts = {'.heic', '.heif'}

        def format_time(ms):
            if ms < 0:
                ms = 0
            s = ms // 1000
            m = s // 60
            s = s % 60
            h = m // 60
            m = m % 60
            if h > 0:
                return f"{h:02d}:{m:02d}:{s:02d}"
            return f"{m:02d}:{s:02d}"

        if ext in video_exts:
            try:
                video_widget = QVideoWidget(dialog)
                video_widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                video_widget.keyPressEvent = close_on_key
                layout.addWidget(video_widget, 1)

                controls_layout = QHBoxLayout()
                slider = QSlider(Qt.Orientation.Horizontal, dialog)
                slider.setRange(0, 0)
                slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                slider.keyPressEvent = close_on_key
                controls_layout.addWidget(slider)

                time_label = QLabel("00:00 / 00:00", dialog)
                time_label.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                time_label.setStyleSheet("QLabel { font-family: 'Courier New', monospace; font-size: 11px; min-width: 80px; border: none; background: transparent; }")
                time_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
                controls_layout.addWidget(time_label)
                layout.addLayout(controls_layout, 0)

                player = QMediaPlayer(dialog)
                audio = QAudioOutput(dialog)
                player.setAudioOutput(audio)
                player.setVideoOutput(video_widget)
                player.setSource(QUrl.fromLocalFile(file_path))

                def update_time_label():
                    time_label.setText(f"{format_time(player.position())} / {format_time(player.duration())}")

                def on_position_changed(position):
                    if not slider.isSliderDown():
                        slider.setValue(position)
                    update_time_label()
                    duration = player.duration()
                    if duration > 0 and position >= duration:
                        player.pause()

                def on_duration_changed(duration):
                    slider.setRange(0, duration)
                    update_time_label()

                def on_media_status_changed(status):
                    if status == QMediaPlayer.MediaStatus.EndOfMedia:
                        player.pause()
                        player.setPosition(player.duration())

                player.positionChanged.connect(on_position_changed)
                player.durationChanged.connect(on_duration_changed)
                player.mediaStatusChanged.connect(on_media_status_changed)
                slider.sliderMoved.connect(player.setPosition)
                slider.valueChanged.connect(lambda val: player.setPosition(val) if slider.isSliderDown() else None)
                slider.sliderReleased.connect(
                    lambda: player.play() if (player.playbackState() != QMediaPlayer.PlaybackState.PlayingState and player.position() < player.duration()) else None
                )
                player.play()
                current_player[0] = player
                dialog.resize(900, 540)
            except Exception as e:
                label = QLabel(f"Erro ao tentar exibir vídeo: {e}", dialog)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(label)
        elif ext in raw_exts:
            try:
                image = None
                if rawpy:
                    try:
                        with rawpy.imread(file_path) as raw:
                            rgb = raw.postprocess()
                            image = Image.fromarray(rgb)
                    except Exception:
                        pass
                if image is None:
                    image = Image.open(file_path)
                image = image.convert("RGB")
                image.thumbnail((800, 600))
                data = image.tobytes("raw", "RGB")
                qimage = QImage(data, image.width, image.height, image.width * 3, QImage.Format.Format_RGB888).copy()
                label = QLabel(dialog)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                label.setPixmap(QPixmap.fromImage(qimage))
                layout.addWidget(label)
            except Exception as e:
                label = QLabel(f"Não foi possível exibir RAW: {e}", dialog)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(label)
            dialog.resize(820, 620)
        elif ext in heic_exts:
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
                image = Image.open(file_path)
                image = image.convert("RGB")
                image.thumbnail((800, 600))
                data = image.tobytes("raw", "RGB")
                qimage = QImage(data, image.width, image.height, image.width * 3, QImage.Format.Format_RGB888).copy()
                label = QLabel(dialog)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                label.setPixmap(QPixmap.fromImage(qimage))
                layout.addWidget(label)
            except Exception as e:
                label = QLabel(f"Não foi possível exibir HEIC: {e}", dialog)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(label)
            dialog.resize(820, 620)
        else:
            label = QLabel(dialog)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pixmap = QPixmap(file_path)
            if not pixmap.isNull():
                label.setPixmap(pixmap.scaled(
                    800, 600, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                ))
            else:
                label.setText("Não foi possível carregar a imagem.")
            layout.addWidget(label)
            dialog.resize(820, 620)

        return True

    def close_on_key(event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Escape):
            dialog.close()
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            if current_player[0]:
                player = current_player[0]
                delta = 10000 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 2000
                new_pos = max(0, player.position() - delta) if event.key() == Qt.Key.Key_Left else min(player.duration(), player.position() + delta)
                player.setPosition(new_pos)
                if player.playbackState() != QMediaPlayer.PlaybackState.PlayingState and new_pos < player.duration():
                    player.play()
        elif event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            if on_step_fn:
                new_idx = on_step_fn(event, index_state[0])
                if new_idx is not None and 0 <= new_idx < len(items):
                    index_state[0] = new_idx
                    load_item(new_idx)
            elif len(items) > 1:
                step = -1 if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up) else 1
                new_idx = (index_state[0] + step) % len(items)
                if load_item(new_idx):
                    index_state[0] = new_idx
        else:
            QDialog.keyPressEvent(dialog, event)

    dialog.keyPressEvent = close_on_key

    if not load_item(index_state[0]):
        return

    def stop_player():
        if current_player[0]:
            try:
                current_player[0].stop()
                current_player[0].setSource(QUrl())
            except Exception:
                pass

    dialog.finished.connect(stop_player)
    dialog.exec()
