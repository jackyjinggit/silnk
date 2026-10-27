import logging
from typing import Optional

from pydantic import BaseModel
from convert.convert_msoffice import ConvertMSOffice


class FileModel:
    filename: str
    convert_state: str
    total_pages: int
    print_copies: int
    print_range_start: int
    print_range_end: int
    print_side: str
    folder: str
    expire: float

    def __init__(self):
        self.print_copies = 1
        self.print_range_start = 1
        self.print_side = "one-sided"


class JwtToken(BaseModel):
    token: object


class FileList(BaseModel):
    fileList: list
    # 用户下单时选择的打印机 device_id（可选）。
    # 不传时为 None，订单流程与原来完全一致（printer_id 留空）。
    printer_id: Optional[str] = None
    # 班级/社团批量：{mode: "unified", names: [...], owner: "..."}
    # names 只作备注；份数由 fileList[0].print_copies 决定。
    batch: Optional[dict] = None
    # 定时打印：预约出纸时间，unix 秒或 "YYYY-MM-DD HH:MM"（可选）。
    # 留空为立即打印（与原来完全一致）；付费后先不入队，到点才放行。
    scheduled_at: Optional[str] = None
    # 共享资料库（可选）：用户自愿把本次打印成品共享到公开资料库，
    # 审核通过后才公开；勾选后本单享 95 折。班级批量不支持共享。
    share_library: Optional[bool] = False
    library_category: Optional[str] = None


class FileBill(FileModel):
    price: float

    def __init__(self):
        super().__init__()
        self.price = 0.0


class RemoveFilename(BaseModel):
    filename: str


class UpdateJob(BaseModel):
    bill_attributes: str


class UpdateState(BaseModel):
    filename: str
    folder: str
    state: str
    authentication: str
    # 打印进度（可选）：已提交份数 / 已打印页数，由打印机端在上报时附带
    progress_copies: Optional[int] = None
    progress_pages: Optional[int] = None


class ReturnResult:
    code: int
    message: str
    data: dict

    def __init__(self, code, message, data):
        self.code = code
        self.message = message
        self.data = data

class ConvertFactory:
    @staticmethod
    def convert(name):
        if name == 'MSOffice':
            return ConvertMSOffice()
        if name == 'WPS':
            # 延迟导入：WPS 转换依赖 pywpsrpc，仅 Linux/WPS 环境需要；
            # Windows 走 MSOffice 分支，不引入该依赖，避免顶层 import 直接崩溃
            from convert.convert_wps import ConvertWPS
            return ConvertWPS()
        if name == 'LibreOffice':
            # 延迟导入且只依赖系统命令 soffice，不需要额外 Python 包
            from convert.convert_libreoffice import ConvertLibreOffice
            return ConvertLibreOffice()
        logging.error('No converter tool found for ' + name)
        return None

class GetJobsAccessLogFilter(logging.Filter):
    def filter(self, record):
        if record.args[2] == '/printer/jobs':
            return False
        return True
