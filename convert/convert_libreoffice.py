"""LibreOffice headless 转换器。

用系统命令 soffice（或 libreoffice）把 Word/Excel 转成 PDF，无需图形界面，
不需要额外 Python 依赖，仅需服务器安装 libreoffice-writer / libreoffice-calc。

调用入口与其它转换器一致：ConvertLibreOffice.convert_docs / convert_excel，
由 ConvertFactory 依据 setting.CONVERT_TOOL == 'LibreOffice' 选择。
"""

import logging
import os
import shutil
import subprocess

from pypdf import PdfReader

from convert.convert_util import ConvertUtil
from global_vars.files_attributes_singleton import files_attributes_singleton

# 不同发行版命令名不同：Debian/Ubuntu 是 soffice，部分镜像只有 libreoffice
SOFFICE_CANDIDATES = ("soffice", "libreoffice")
# 单文件转换超时（秒），防止坏文件把后台任务卡死
CONVERT_TIMEOUT_SECONDS = 120


def soffice_bin():
    """定位 soffice 可执行文件，未安装时返回 None。"""
    for name in SOFFICE_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def _set_state(files_attributes_global, directory, filename, state):
    """更新转换状态并写回单例，返回该文件属性对象（找不到返回 None）。"""
    files_attributes = files_attributes_global.data.get(directory) or []
    for file_attributes in files_attributes:
        if file_attributes.filename == filename:
            file_attributes.convert_state = state
            return file_attributes
    return None


class ConvertLibreOffice(ConvertUtil):

    @staticmethod
    def _convert(directory, filename):
        """统一转换流程：soffice 转 PDF → 回读页数 → 更新单例状态。"""
        files_attributes_global = files_attributes_singleton()
        _set_state(files_attributes_global, directory, filename, 'processing')

        soffice = soffice_bin()
        if soffice is None:
            logging.error("LibreOffice 未安装（找不到 soffice），无法转换 Word/Excel")
            _set_state(files_attributes_global, directory, filename, 'error')
            return

        raw = os.path.abspath(f"uploads/{directory}/raw/{filename}")
        converted_dir = os.path.abspath(f"uploads/{directory}/converted")
        os.makedirs(converted_dir, exist_ok=True)
        output_filename = filename.rsplit(".", 1)[0] + '.pdf'
        output_pdf = os.path.join(converted_dir, output_filename)

        try:
            subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--norestore",
                    # 服务环境 HOME 可能不可写，显式指定独立的用户配置目录，避免并发锁冲突
                    f"-env:UserInstallation=file:///tmp/lo_profile_{os.getpid()}",
                    "--convert-to", "pdf",
                    "--outdir", converted_dir,
                    raw,
                ],
                check=True,
                timeout=CONVERT_TIMEOUT_SECONDS,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if not os.path.exists(output_pdf):
                raise RuntimeError("soffice 未生成 PDF 文件，请检查源文件是否有效")
            reader = PdfReader(output_pdf)
            file_attributes = _set_state(
                files_attributes_global, directory, filename, 'success')
            # 页数只有在 PDF 生成后才拿得到；拿不到就保持原值，不编造
            if file_attributes is not None:
                file_attributes.total_pages = len(reader.pages)
                file_attributes.print_range_end = len(reader.pages)
        except Exception as e:
            _set_state(files_attributes_global, directory, filename, 'error')
            logging.error(f"Failed to convert file to PDF: {filename}")
            logging.error(f"Exception: {e}")

    @staticmethod
    def convert_docs(directory, filename):
        ConvertLibreOffice._convert(directory, filename)

    @staticmethod
    def convert_excel(directory, filename):
        ConvertLibreOffice._convert(directory, filename)
