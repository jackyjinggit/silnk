import logging
import os
import sys
from pypdf import PdfReader

from convert.convert_util import ConvertUtil
from global_vars.files_attributes_singleton import files_attributes_singleton
if sys.platform.startswith('win'):
    import pythoncom
    import win32com.client

class ConvertMSOffice(ConvertUtil):

    def convert_docs(directory, filename):
        files_attributes_global = files_attributes_singleton()
        files_attributes = files_attributes_global.data.get(directory)

        for file_attributes in files_attributes:
            if file_attributes.filename == filename:
                file_attributes.convert_state = 'processing'
        files_attributes_global.data.update({directory: files_attributes})

        pythoncom.CoInitialize()
        wdFormatPDF = 17
        word = win32com.client.Dispatch("Word.Application")

        input_doc = os.path.abspath(f"uploads/{directory}/raw/{filename}")
        output_filename = filename.rsplit(".", 1)[0] + '.pdf'
        output_pdf = os.path.abspath(f"uploads/{directory}/converted/{output_filename}")

        # 说明：win32com 仅在 Windows 平台导入（见文件顶部平台判断）。
        # 非 Windows 平台调用本函数会因缺少 win32com 而失败，属「待做」项（见 GET /api/status）。
        doc = None
        try:
            doc = word.Documents.Open(input_doc)
            doc.SaveAs(output_pdf, FileFormat=wdFormatPDF)
            files_attributes = files_attributes_global.data.get(directory)
            for file_attributes in files_attributes:
                if file_attributes.filename == filename:
                    file_attributes.convert_state = 'success'
                    converted_filename = file_attributes.filename.rsplit(".", 1)[0] + '.pdf'
                    reader = PdfReader(f"uploads/{directory}/converted/{converted_filename}")
                    file_attributes.total_pages = len(reader.pages)
                    file_attributes.print_range_end = len(reader.pages)

            files_attributes_global.data.update({directory: files_attributes})
        except Exception as e:
            for file_attributes in files_attributes:
                if file_attributes.filename == filename:
                    file_attributes.convert_state = 'error'
            files_attributes_global.data.update({directory: files_attributes})
            logging.error(f"Failed to convert file to PDF: {filename}")
            logging.error(f"Exception: {e}")
        finally:
            if doc is not None:
                doc.Close()
            word.Quit()
            pythoncom.CoUninitialize()

    def convert_excel(directory, filename):
        files_attributes_global = files_attributes_singleton()
        files_attributes = files_attributes_global.data.get(directory)

        for file_attributes in files_attributes:
            if file_attributes.filename == filename:
                file_attributes.convert_state = 'processing'
        files_attributes_global.data.update({directory: files_attributes})

        pythoncom.CoInitialize()
        excel = win32com.client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = 0

        input_excel = os.path.abspath(f"uploads/{directory}/raw/{filename}")
        output_filename = filename.rsplit(".", 1)[0] + '.pdf'
        output_pdf = os.path.abspath(f"uploads/{directory}/converted/{output_filename}")

        # 原代码在此处提前 excel.Quit()，会让随后 Workbooks.Open 作用于已退出的实例，
        # Excel 转换必然失败；进程退出统一交给 finally 处理。
        sheets = None
        try:
            sheets = excel.Workbooks.Open(input_excel, False)
            sheets.ExportAsFixedFormat(0, output_pdf)

            files_attributes = files_attributes_global.data.get(directory)
            for file_attributes in files_attributes:
                if file_attributes.filename == filename:
                    file_attributes.convert_state = 'success'
                    converted_filename = file_attributes.filename.rsplit(".", 1)[0] + '.pdf'
                    reader = PdfReader(f"uploads/{directory}/converted/{converted_filename}")
                    file_attributes.total_pages = len(reader.pages)
                    file_attributes.print_range_end = len(reader.pages)

            files_attributes_global.data.update({directory: files_attributes})
        except Exception as e:
            for file_attributes in files_attributes:
                if file_attributes.filename == filename:
                    file_attributes.convert_state = 'error'
            files_attributes_global.data.update({directory: files_attributes})
            logging.error(f"Failed to convert file to PDF: {filename}")
            logging.error(f"Exception: {e}")
        finally:
            if sheets is not None:
                sheets.Close()
            excel.Quit()
            pythoncom.CoUninitialize()
