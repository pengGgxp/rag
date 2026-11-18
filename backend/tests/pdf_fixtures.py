"""Small, real PDFs for encryption, ingestion and browser regression fixtures."""
import io

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def make_pdf(*, password=None, page_count=2, algorithm="AES-128"):
    writer = PdfWriter()
    for index in range(1, page_count + 1):
        page = writer.add_blank_page(width=600, height=800)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 18 Tf 50 740 Td (Page {index} evidence) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    if password is not None:
        writer.encrypt(password, owner_password="owner-only-password", algorithm=algorithm)
    output = io.BytesIO()
    writer.write(output)
    writer.close()
    return output.getvalue()
