''' thumbnails.py - Centraliza toda a lógica de thumbnails do Vox Imago

 Responsável por:
 - Gerenciar a renderização de miniaturas (thumbnails) para arquivos locais
 - Gerar thumbnails para imagens, vídeos, PDFs e arquivos RAW
 - Gerenciar e consultar o cache de thumbnails
 - Fornecer ícones genéricos para tipos de arquivo
 - Classes principais: FileListDelegate (delegate para listas), ThumbnailManager (geração), ThumbnailCache (cache)
 
'''
from PyQt6.QtCore import QRect, QRunnable
from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QPixmap, QFont, QPainter, QBrush, QColor, QImage, QPen
from PyQt6.QtWidgets import QStyledItemDelegate, QStyle
from PyQt6.QtCore import QObject
import os
import hashlib
import mimetypes
import logging
import requests
import threading
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass


THUMBNAIL_CACHE_DIR = 'assets/thumbnail_cache'
_media_lock = threading.Lock()
_thumbnail_failure_lock = threading.Lock()
_thumbnail_failure_counts = {}


def _rotation_degrees(file_item):
    try:
        value = int((file_item or {}).get('thumbnailRotation', 0) or 0) % 360
    except (TypeError, ValueError):
        value = 0
    return value if value in (0, 90, 180, 270) else 0


def _apply_rotation_override(cache_path, file_item):
    """Aplica uma unica vez o override ao arquivo novo de uma chave rotacionada."""
    rotation = _rotation_degrees(file_item)
    if not cache_path or not rotation:
        return cache_path
    try:
        from PIL import Image
        with Image.open(cache_path) as image:
            rotated = image.rotate(-rotation, expand=True)
            if os.path.splitext(cache_path)[1].lower() in ('.jpg', '.jpeg'):
                if rotated.mode not in ('RGB', 'L'):
                    rotated = rotated.convert('RGB')
                rotated.save(cache_path, 'JPEG')
            else:
                rotated.save(cache_path, 'PNG')
    except Exception as exc:
        _record_thumbnail_failure(
            'rotacao', f'[THUMB][ROTACAO][ERRO] {cache_path}: {exc}'
        )
    return cache_path


def _record_thumbnail_failure(category, message, *args):
    """Mantem o detalhe no DEBUG e resume repeticoes no log normal."""
    logging.debug(message, *args)
    with _thumbnail_failure_lock:
        count = _thumbnail_failure_counts.get(category, 0) + 1
        _thumbnail_failure_counts[category] = count

    if count in (1, 10, 100, 1000) or count % 10000 == 0:
        logging.warning(
            "Falhas de miniatura (%s): %d nesta execucao. "
            "Use VOXIMAGO_LOG_LEVEL=DEBUG para detalhes por arquivo.",
            category,
            count,
        )



class ThumbnailTask(QRunnable):
    def __init__(self, worker):
        super().__init__()
        self.worker = worker

    def run(self):
        self.worker.run()


class ThumbnailWorker(QObject):
    finished = pyqtSignal(bytes, str)

    def __init__(self, thumbnail_url, file_item, parent=None):
        super().__init__(parent)
        self.thumbnail_url = thumbnail_url
        self.file_item = file_item
        os.makedirs(THUMBNAIL_CACHE_DIR, exist_ok=True)

    def run(self):
        try:
            cache_key = ThumbnailCache.get_thumbnail_cache_key(self.file_item)
            thumbnail_path = os.path.join(
                THUMBNAIL_CACHE_DIR, f"{cache_key}.jpg")
            if os.path.exists(thumbnail_path):
                with open(thumbnail_path, 'rb') as f:
                    data = f.read()
                self.finished.emit(data, thumbnail_path)
                return
            response = requests.get(self.thumbnail_url)
            if response.status_code == 200:
                with open(thumbnail_path, 'wb') as f:
                    f.write(response.content)
                _apply_rotation_override(thumbnail_path, self.file_item)
                with open(thumbnail_path, 'rb') as f:
                    rendered_content = f.read()
                self.finished.emit(rendered_content, thumbnail_path)
            else:
                self.finished.emit(b'', '')
        except Exception:
            self.finished.emit(b'', '')


class FileListDelegate(QStyledItemDelegate):
    requestThumbnail = pyqtSignal(dict)

    def __init__(self, parent=None, indexer=None):
        super().__init__(parent)
        self.indexer = indexer

    def paint(self, painter, option, index):
        file_item = index.data(Qt.ItemDataRole.UserRole)
        if not file_item:
            return super().paint(painter, option, index)

        painter.save()
        rect = option.rect
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, option.palette.highlight())
        else:
            painter.fillRect(rect, option.palette.base())

        from src.ui.staging_queue import StagingQueue
        queue = StagingQueue()
        raw_fid = file_item.get('file_id') or file_item.get('id') or ''
        raw_path = file_item.get('path', '')
        norm_fid = os.path.normcase(os.path.normpath(raw_fid)) if raw_fid else ''
        norm_path = os.path.normcase(os.path.normpath(raw_path)) if raw_path else ''

        is_deleted = False
        is_staged_green = False
        staged_text = ""

        for it in queue.items:
            it_fid = os.path.normcase(os.path.normpath(it.file_id)) if it.file_id else ''
            it_path = os.path.normcase(os.path.normpath(it.path)) if it.path else ''
            matches = (it_fid and (it_fid == norm_fid or it_fid == norm_path)) or (it_path and (it_path == norm_path or it_path == norm_fid))
            
            if matches:
                if it.action_type == 'delete':
                    is_deleted = True
                elif it.action_type == 'move':
                    is_staged_green = True
                    staged_text = "🟢 Mover (Fila)"
                elif it.action_type in ('set_description', 'add_tags', 'remove_tags', 'rename'):
                    is_staged_green = True
                    staged_text = "🟢 Em Fila"

        if is_deleted:
            painter.setOpacity(0.3)

        parent_view = self.parent()
        is_grid = hasattr(parent_view, 'viewMode') and parent_view.viewMode() == parent_view.ViewMode.IconMode

        pixmap = None
        is_dir = file_item.get('mimeType') in ('folder', 'application/vnd.google-apps.folder')
        if not is_dir:
            local_path = file_item.get('physical_path') or file_item.get('orig_path') or file_item.get('path') or ''
            extension = os.path.splitext(local_path)[1].lower() if local_path else ''
            supported_extensions = {'.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm', '.m4v',
                                    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp',
                                    '.raw', '.cr2', '.nef', '.arw', '.dng', '.orf', '.rw2', '.pef', '.srw', '.raf',
                                    '.pdf', '.heic', '.heif'}

            if extension in supported_extensions:
                if ThumbnailCache.is_thumbnail_cached(file_item):
                    cached = ThumbnailCache.get_existing_thumbnail_cache_path(file_item)
                    p = QPixmap(cached)
                    if not p.isNull():
                        pixmap = p
                else:
                    try:
                        self.requestThumbnail.emit(file_item)
                    except Exception:
                        pass

        if pixmap is None:
            pixmap = ThumbnailManager.get_generic_thumbnail(
                file_item.get('mimeType'))

        source = file_item.get('source', 'local')
        source_name = "Cloud" if source == 'drive' else "Local"

        if is_grid:
            icon_size = parent_view.iconSize().width()
            pixmap = pixmap.scaled(
                icon_size, icon_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            x_icon = rect.left() + (rect.width() - icon_size) // 2
            y_icon = rect.top() + 12
            painter.drawPixmap(x_icon, y_icon, pixmap)

            name = file_item.get('name', 'Nome Desconhecido')
            max_name_width = rect.width() - 16
            font = QFont("Arial", 10, QFont.Weight.Bold)
            painter.setFont(font)
            metrics = painter.fontMetrics()
            elided_name = metrics.elidedText(
                name, Qt.TextElideMode.ElideRight, max_name_width)
            name_height = metrics.height()
            name_bg_rect = QRect(
                rect.left()+8, rect.bottom() - name_height-12, rect.width()-16, name_height+6)
            painter.setBrush(Qt.GlobalColor.black)
            painter.setOpacity(0.45)
            painter.setPen(Qt.GlobalColor.transparent)
            painter.drawRect(name_bg_rect)
            painter.setOpacity(1.0)
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(
                name_bg_rect, Qt.AlignmentFlag.AlignCenter, elided_name)

            painter.setFont(QFont("Arial", 8))
            painter.setPen(Qt.GlobalColor.gray)
            painter.drawText(rect.left()+8, rect.bottom()-6, source_name)
        else:
            icon_size = 48
            pixmap = pixmap.scaled(
                icon_size, icon_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            x_icon = rect.left() + 8
            y_icon = rect.top() + (rect.height() - icon_size) // 2
            painter.drawPixmap(x_icon, y_icon, pixmap)

            name = file_item.get('name', 'Nome Desconhecido')
            font = QFont("Arial", 10)
            painter.setFont(font)
            painter.setPen(option.palette.text().color())
            painter.drawText(rect.left()+64, rect.top()+22, name)

            painter.setFont(QFont("Arial", 8))
            painter.setPen(Qt.GlobalColor.gray)
            painter.drawText(rect.left()+64, rect.top()+38, f"({source_name})")

        # Desenhar borda verde e badge de status para itens na Fila
        if is_staged_green:
            pen = QPen(QColor("#28A745"), 2)
            painter.setPen(pen)
            painter.setBrush(Qt.GlobalColor.transparent)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 4, 4)

            if staged_text:
                badge_w = 96
                badge_rect = QRect(rect.right() - badge_w - 4, rect.top() + 4, badge_w, 18)
                painter.setBrush(QColor("#28A745"))
                painter.setPen(Qt.GlobalColor.transparent)
                painter.drawRoundedRect(badge_rect, 4, 4)
                painter.setPen(QColor("#FFFFFF"))
                painter.setFont(QFont("Arial", 8, QFont.Weight.Bold))
                painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, staged_text)

        painter.restore()


    def editorEvent(self, event, model, option, index):
        return super().editorEvent(event, model, option, index)

    def sizeHint(self, option, index):
        parent_view = self.parent()
        if hasattr(parent_view, 'viewMode') and parent_view.viewMode() == parent_view.ViewMode.IconMode:
            grid_size = parent_view.gridSize()
            return grid_size
        return QSize(option.rect.width(), 60)


class ThumbnailManager:

    def generate_local_thumbnail(file_item, size=(256, 256)):
        local_path = file_item.get('path', '')
        extension = os.path.splitext(
            local_path)[1].lower() if local_path else ''

        if not local_path or os.path.isdir(local_path):
            return None

        if ThumbnailCache.is_thumbnail_cached(file_item):
            return ThumbnailCache.get_existing_thumbnail_cache_path(file_item)

        video_extensions = {'.mp4', '.avi', '.mov',
                            '.mkv', '.wmv', '.flv', '.webm', '.m4v'}
        image_extensions = {'.jpg', '.jpeg', '.png',
                            '.gif', '.bmp', '.tiff', '.webp', '.heic', '.heif'}
        raw_extensions = {'.raw', '.cr2', '.nef', '.arw',
                          '.dng', '.orf', '.rw2', '.pef', '.srw', '.raf'}
        pdf_extensions = {'.pdf'}

        if extension in raw_extensions:
            raw_result = ThumbnailManager.generate_local_raw_thumbnail(
                file_item, size[0])
            if raw_result:
                return _apply_rotation_override(raw_result, file_item)

        if extension in image_extensions:
            image_result = ThumbnailManager.generate_local_image_thumbnail(
                file_item, size[0])
            if image_result:
                return _apply_rotation_override(image_result, file_item)

        if extension in pdf_extensions:
            pdf_result = ThumbnailManager.generate_local_pdf_thumbnail(
                file_item, size[0])
            if pdf_result:
                return _apply_rotation_override(pdf_result, file_item)

        if extension in video_extensions:
            video_result = ThumbnailManager.generate_local_video_thumbnail(
                file_item, size[0]
            )
            return _apply_rotation_override(video_result, file_item)

        return None

    def generate_local_video_thumbnail(file_item, base_size=256):
        try:
            local_path = file_item.get('physical_path') or file_item.get('orig_path') or file_item.get('path')
            if not local_path or not os.path.exists(local_path):
                if file_item.get('orig_path') and os.path.exists(file_item['orig_path']):
                    local_path = file_item['orig_path']
                else:
                    return None
            if os.path.isdir(local_path):
                return None

            if ThumbnailCache.is_thumbnail_cached(file_item):
                return ThumbnailCache.get_existing_thumbnail_cache_path(file_item)

            logging.debug(
                f"[THUMB][VIDEO] Tentando gerar thumbnail para: {local_path}")
            if os.path.isdir(local_path):
                logging.debug(
                    f"[THUMB][VIDEO][IGNORADO] Caminho é um diretório, ignorando: {local_path}")
                return None
            _, ext = os.path.splitext(local_path)
            ext = ext.lower()
            image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp',
                          '.heic', '.arw', '.cr2', '.nef', '.dng', '.raf', '.orf', '.srw'}
            if ext == '.lnk' or ext in image_exts:
                logging.debug(
                    f"[THUMB][VIDEO][IGNORADO] Arquivo ignorado por extensão: {local_path} ({ext})")
                return None
            mime = (file_item.get('mimeType') or '').lower()
            guessed, _ = mimetypes.guess_type(local_path)
            is_video = (mime.startswith('video/')
                        or (guessed and guessed.startswith('video/')))
            video_exts = {'.mp4', '.mov', '.avi',
                          '.mkv', '.wmv', '.flv', '.webm', '.m4v'}
            if not is_video and os.path.splitext(local_path)[1].lower() not in video_exts:
                logging.debug(
                    f"[THUMB][VIDEO][IGNORADO] Não é vídeo: {local_path} (mime: {mime}, guessed: {guessed})")
                return None
            try:
                os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = '-loglevel quiet'
                import cv2
                import numpy as np
                try:
                    if hasattr(cv2, 'setLogLevel'):
                        cv2.setLogLevel(getattr(cv2, 'LOG_LEVEL_SILENT', 0))
                    elif hasattr(cv2, 'utils') and hasattr(cv2.utils, 'logging') and hasattr(cv2.utils.logging, 'setLogLevel'):
                        cv2.utils.logging.setLogLevel(0)
                except Exception as log_e:
                    _record_thumbnail_failure(
                        "video-opencv-logging",
                        f"[THUMB][VIDEO][WARN] Não foi possível silenciar logs do OpenCV: {log_e}")
            except Exception as e:
                _record_thumbnail_failure(
                    "video-dependencias",
                    f"[THUMB][VIDEO][ERRO] Falha ao importar OpenCV/numpy: {e}")
                return None
            with _media_lock:
                cap = cv2.VideoCapture(local_path)
                if not cap.isOpened():
                    _record_thumbnail_failure(
                        "video-abertura",
                        f"[THUMB][VIDEO][ERRO] Não foi possível abrir o vídeo: {local_path}")
                    return None
                ret, frame = cap.read()
                if not ret:
                    logging.debug(
                        f"[THUMB][VIDEO][WARN] Frame inicial não lido, tentando 500ms...")
                    cap.set(cv2.CAP_PROP_POS_MSEC, 500)
                    ret, frame = cap.read()
                    if not ret:
                        _record_thumbnail_failure(
                            "video-frame",
                            f"[THUMB][VIDEO][ERRO] Não foi possível ler frame do vídeo: {local_path}")
                        cap.release()
                        return None
                cap.release()
                if frame is None or frame.size == 0:
                    return None
                try:
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    frame_rgb = np.ascontiguousarray(frame_rgb)
                except Exception as e:
                    _record_thumbnail_failure(
                        "video-conversao",
                        f"[THUMB][VIDEO][ERRO] Falha ao converter frame para RGB: {e}")
                    return None
                h, w, ch = frame_rgb.shape
                bytes_per_line = ch * w
                try:
                    img = QImage(frame_rgb.data, w, h, bytes_per_line,
                                 QImage.Format.Format_RGB888).copy()
                except Exception as e:
                    _record_thumbnail_failure(
                        "video-qimage",
                        f"[THUMB][VIDEO][ERRO] Falha ao criar QImage: {e}")
                    return None

            img = img.scaled(base_size, base_size, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
            cache_path = ThumbnailCache.get_thumbnail_cache_path(
                file_item, 'png')
            ThumbnailCache.ensure_thumbnail_cache_dir()
            ok = img.save(cache_path, 'PNG')
            if ok:
                logging.debug(
                    f"[THUMB][VIDEO] Thumbnail salva em: {cache_path}")
            else:
                _record_thumbnail_failure(
                    "video-gravacao",
                    f"[THUMB][VIDEO][ERRO] Falha ao salvar thumbnail em: {cache_path}")
            return cache_path if ok else None
        except Exception as e:
            _record_thumbnail_failure("video", f'[THUMB][VIDEO][ERRO] {e}')
            return None

    def generate_local_pdf_thumbnail(file_item, base_size=256):
        try:
            local_path = file_item.get('physical_path') or file_item.get('orig_path') or file_item.get('path')
            if not local_path or not os.path.exists(local_path):
                if file_item.get('orig_path') and os.path.exists(file_item['orig_path']):
                    local_path = file_item['orig_path']
                else:
                    return None
            _, ext = os.path.splitext(local_path)
            if (file_item.get('mimeType') or '').lower() != 'application/pdf' and ext.lower() != '.pdf':
                return None
            try:
                import fitz
            except Exception:
                return None
            doc = fitz.open(local_path)
            if doc.page_count == 0:
                return None
            page = doc.load_page(0)
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            cache_path = ThumbnailCache.get_thumbnail_cache_path(
                file_item, 'png')
            ThumbnailCache.ensure_thumbnail_cache_dir()
            pix.save(cache_path)
            img = QImage(cache_path)
            if img.isNull():
                return cache_path
            img = img.scaled(base_size, base_size, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
            img.save(cache_path, 'PNG')
            return cache_path
        except Exception as e:
            _record_thumbnail_failure("pdf", f'[THUMB][PDF][ERRO] {e}')
            return None

    def generate_local_image_thumbnail(file_item, base_size=256):
        try:
            if not file_item:
                return None
            mime = (file_item.get('mimeType') or '').lower()
            local_path = file_item.get('physical_path') or file_item.get('orig_path') or file_item.get('path')
            if not local_path or not os.path.exists(local_path):
                if file_item.get('orig_path') and os.path.exists(file_item['orig_path']):
                    local_path = file_item['orig_path']
                else:
                    return None
            if not mime.startswith('image/'):
                guessed, _ = mimetypes.guess_type(local_path)
                if not guessed or not guessed.startswith('image/'):
                    return None

            # Pillow corrige EXIF de camera antes de redimensionar. QImage puro
            # nem sempre aplica essa orientacao de forma consistente entre
            # plugins/formatos.
            try:
                from PIL import Image, ImageOps
                with _media_lock:
                    with Image.open(local_path) as pil_source:
                        pil_img = ImageOps.exif_transpose(pil_source)
                        if pil_img.mode not in ('RGB', 'RGBA'):
                            pil_img = pil_img.convert('RGBA' if 'A' in pil_img.getbands() else 'RGB')
                        pil_img.thumbnail((base_size, base_size), Image.LANCZOS)
                        cache_path = ThumbnailCache.get_thumbnail_cache_path(
                            file_item, 'png'
                        )
                        ThumbnailCache.ensure_thumbnail_cache_dir()
                        pil_img.save(cache_path, 'PNG')
                        return cache_path
            except Exception:
                pass

            img = QImage(local_path)
            if img.isNull():
                _, ext = os.path.splitext(local_path)
                if ext.lower() in ['.heic', '.heif']:
                    try:
                        from PIL import Image
                        import numpy as np
                        pil_img = Image.open(local_path)
                        pil_img = pil_img.convert('RGB')
                        pil_img.thumbnail(
                            (base_size, base_size), Image.LANCZOS)
                        arr = np.array(pil_img)
                        h, w, ch = arr.shape
                        bytes_per_line = ch * w
                        img = QImage(arr.data, w, h, bytes_per_line,
                                     QImage.Format.Format_RGB888).copy()
                    except Exception as heic_e:
                        _record_thumbnail_failure(
                            "imagem-heic",
                            f'[THUMB][IMG][HEIC] Falha ao abrir HEIC/HEIF: {heic_e}')
                        return None
                else:
                    return None
            else:
                img = img.scaled(base_size, base_size, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            cache_path = ThumbnailCache.get_thumbnail_cache_path(
                file_item, 'png')
            ThumbnailCache.ensure_thumbnail_cache_dir()
            ok = img.save(cache_path, 'PNG')
            if not ok:
                return None
            return cache_path
        except Exception as e:
            _record_thumbnail_failure("imagem", f'[THUMB][IMG][ERRO] {e}')
            return None

    def generate_local_raw_thumbnail(file_item, base_size=256):
        try:
            import rawpy
            import numpy as np
            from PyQt6.QtGui import QImage
            local_path = file_item.get('physical_path') or file_item.get('orig_path') or file_item.get('path')
            if not local_path or not os.path.exists(local_path):
                if file_item.get('orig_path') and os.path.exists(file_item['orig_path']):
                    local_path = file_item['orig_path']
                else:
                    _record_thumbnail_failure(
                        "raw-caminho",
                        f'[THUMB][RAW][ERRO] Caminho inválido ou arquivo não existe: {local_path}')
                    return None
            raw_exts = ['.raw', '.arw', '.cr2', '.nef',
                        '.dng', '.raf', '.orf', '.srw', '.rw2', '.pef']
            if not any(local_path.lower().endswith(ext) for ext in raw_exts):
                _record_thumbnail_failure(
                    "raw-extensao",
                    f'[THUMB][RAW][ERRO] Extensão não suportada para RAW: {local_path}')
                return None
            try:
                with _media_lock:
                    from PIL import Image
                    pil_img = None
                    try:
                        # 1. Tentar com rawpy
                        with rawpy.imread(local_path) as raw:
                            thumb = raw.extract_thumb()
                            if thumb.format == rawpy.ThumbFormat.JPEG:
                                import imageio.v3 as iio
                                img = iio.imread(thumb.data)
                                pil_img = Image.fromarray(img)
                            elif thumb.format == rawpy.ThumbFormat.BITMAP:
                                from io import BytesIO
                                pil_img = Image.open(BytesIO(thumb.data))
                                pil_img = pil_img.convert('RGB')
                            else:
                                raise ValueError("Formato de miniatura embutido nao suportado pelo rawpy")
                    except Exception as raw_err:
                        # 2. Fallback direto para Pillow (Pillow consegue abrir muitos DNGs/TIFFs)
                        logging.debug(f"[THUMB][RAW][INFO] Falha ao extrair miniatura com rawpy ({raw_err}). Tentando Pillow para: {local_path}")
                        try:
                            pil_img = Image.open(local_path)
                            pil_img = pil_img.convert('RGB')
                        except Exception as pil_err:
                            _record_thumbnail_failure(
                                "raw-decodificacao",
                                f"[THUMB][RAW][ERRO] Falha em todos os metodos (rawpy e Pillow) para {local_path}: {pil_err}",
                            )
                            return None

                    if pil_img is None:
                        return None

                    try:
                        pil_img.thumbnail((base_size, base_size), Image.LANCZOS)
                        img = np.array(pil_img)
                    except Exception as resize_e:
                        _record_thumbnail_failure(
                            "raw-redimensionamento",
                            f'[THUMB][RAW][ERRO] Falha ao redimensionar thumb: {resize_e} | arquivo: {local_path}')
                        return None

                    if img is None or img.size == 0 or len(img.shape) != 3:
                        _record_thumbnail_failure(
                            "raw-imagem-invalida",
                            f'[THUMB][RAW][ERRO] Imagem extraída do RAW/Pillow é inválida. | arquivo: {local_path}')
                        return None

                    img = np.ascontiguousarray(img)
                    h, w, ch = img.shape
                    bytes_per_line = ch * w
                    try:
                        qimg = QImage(img.data, w, h, bytes_per_line,
                                      QImage.Format.Format_RGB888).copy()
                    except Exception as qimg_e:
                        _record_thumbnail_failure(
                            "raw-qimage",
                            f'[THUMB][RAW][ERRO] Falha ao criar QImage: {qimg_e} | arquivo: {local_path}')
                        return None

                    cache_path = ThumbnailCache.get_thumbnail_cache_path(
                        file_item, 'png')
                    ThumbnailCache.ensure_thumbnail_cache_dir()
                    if not qimg.save(cache_path, 'PNG'):
                        _record_thumbnail_failure(
                            "raw-gravacao",
                            f'[THUMB][RAW][ERRO] Falha ao salvar thumbnail PNG: {cache_path} | arquivo: {local_path}')
                        return None
                    return cache_path
            except Exception as raw_e:
                _record_thumbnail_failure(
                    "raw-leitura",
                    f'[THUMB][RAW][ERRO] Falha ao ler arquivo RAW: {raw_e} | arquivo: {local_path}')
                return None
        except Exception as e:
            _record_thumbnail_failure(
                "raw",
                f'[THUMB][RAW][ERRO] {e} | arquivo: {file_item.get("path") if file_item else None}')
            return None

    def get_generic_thumbnail(mime_type, size=(48, 48)):
        icon_dir = os.path.abspath(os.path.join(
            os.path.dirname(__file__), '..', '..', 'assets', 'icons'))

        icon_map = {
            'folder': 'folder.png',
            'application/vnd.google-apps.folder': 'folder.png',
            'application/pdf': 'pdf.png',
            'application/vnd.google-apps.document': 'doc.png',
            'application/msword': 'doc.png',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'doc.png',
            'application/vnd.google-apps.spreadsheet': 'xls.png',
            'application/vnd.ms-excel': 'xls.png',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'xls.png',
            'application/vnd.google-apps.presentation': 'ppt.png',
            'application/vnd.ms-powerpoint': 'ppt.png',
            'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'ppt.png',
            'raw': 'raw.png',
            'image/x-raw': 'raw.png',
        }
        raw_mimes = [
            'image/x-raw', 'image/arw', 'image/cr2', 'image/nef', 'image/dng', 'image/raf', 'image/orf', 'image/srw'
        ]
        if mime_type in raw_mimes:
            icon_file = 'raw.png'
        elif mime_type.startswith('image/'):
            icon_file = 'image.png'
        else:
            icon_file = icon_map.get(mime_type, 'file.png')

        icon_path = os.path.join(icon_dir, icon_file)
        if os.path.exists(icon_path):
            pixmap = QPixmap(icon_path)
            return pixmap.scaled(size[0], size[1], Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        else:
            pixmap = QPixmap(size[0], size[1])
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(QBrush(QColor(100, 100, 100)))
            painter.drawRect(0, 0, size[0], size[1])
            painter.end()
            return pixmap


class ThumbnailCache:
    @staticmethod
    def get_thumbnail_cache_key(file_item):
        rotation = _rotation_degrees(file_item)
        if file_item.get('source') == 'local':
            path = file_item.get('physical_path') or file_item.get('orig_path') or file_item.get('path') or ''
            mtime = str(file_item.get('modifiedTime', ''))
            size = str(file_item.get('size', ''))
            key = hashlib.sha1(
                f"{path}|{mtime}|{size}|rotation={rotation}".encode()
            ).hexdigest()
        else:
            file_id = file_item.get('id', '')
            modified_time = str(file_item.get('modifiedTime', ''))
            key = hashlib.sha1(
                f"{file_id}|{modified_time}|rotation={rotation}".encode()
            ).hexdigest()
        return key

    @staticmethod
    def ensure_thumbnail_cache_dir():
        try:
            if not os.path.exists(THUMBNAIL_CACHE_DIR):
                os.makedirs(THUMBNAIL_CACHE_DIR, exist_ok=True)
        except Exception as e:
            print(f"❌ Erro criando diretório de cache: {e}")
            pass
        return THUMBNAIL_CACHE_DIR

    @staticmethod
    def get_thumbnail_cache_path(file_item, ext='png'):
        ThumbnailCache.ensure_thumbnail_cache_dir()
        key = ThumbnailCache.get_thumbnail_cache_key(file_item)
        return os.path.join(THUMBNAIL_CACHE_DIR, f"{key}.{ext}")

    @staticmethod
    def is_thumbnail_cached(file_item):
        path_jpg = ThumbnailCache.get_thumbnail_cache_path(file_item, 'jpg')
        path_png = ThumbnailCache.get_thumbnail_cache_path(file_item, 'png')
        hit = False
        if os.path.exists(path_png):
            hit = True
        if os.path.exists(path_jpg):
            hit = True
        return hit

    @staticmethod
    def get_existing_thumbnail_cache_path(file_item):
        path_png = ThumbnailCache.get_thumbnail_cache_path(file_item, 'png')
        path_jpg = ThumbnailCache.get_thumbnail_cache_path(file_item, 'jpg')
        if os.path.exists(path_png):
            return path_png
        if os.path.exists(path_jpg):
            return path_jpg
        return path_png
