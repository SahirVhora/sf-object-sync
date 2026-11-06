from openpyxl import load_workbook
from src.report_generator import generate_report


def test_input_codes_are_literal_text_in_saved_workbook(tmp_path):
    path = generate_report(str(tmp_path), {}, [], [{"input_code": "=1+1"}], [])
    book = load_workbook(path)
    cell = book["Object Detail"]["B2"]
    assert cell.value == "=1+1"
    assert cell.data_type == "s"
    book.close()
