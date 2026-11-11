"""Small text-PDF fixtures without an additional test dependency."""
from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject


def make_pdf(texts, *, encrypted=False):
    writer = PdfWriter()
    characters = sorted(set(''.join(texts)))
    cmap = DecodedStreamObject()
    mappings = '\n'.join(f'<{ord(char):04x}> <{ord(char):04x}>' for char in characters)
    cmap.set_data((f'/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n'
                   f'/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n'
                   f'/CMapName /Test def /CMapType 2 def\n'
                   f'1 begincodespacerange <0000> <FFFF> endcodespacerange\n'
                   f'{len(characters)} beginbfchar\n{mappings}\nendbfchar\n'
                   f'endcmap CMapName currentdict /CMap defineresource pop end end').encode())
    font = DictionaryObject({
        NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type0'),
        NameObject('/BaseFont'): NameObject('/Test'), NameObject('/Encoding'): NameObject('/Identity-H'),
        NameObject('/ToUnicode'): writer._add_object(cmap),
        NameObject('/DescendantFonts'): ArrayObject([DictionaryObject({
            NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/CIDFontType2'),
            NameObject('/BaseFont'): NameObject('/Test'),
        })]),
    })
    for text in texts:
        page = writer.add_blank_page(width=595, height=842)
        if text:
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            content = DecodedStreamObject()
            content.set_data(f'BT /F1 12 Tf 50 790 Td <{text.encode("utf-16-be").hex()}> Tj ET'.encode())
            page[NameObject('/Contents')] = writer._add_object(content)
    if encrypted:
        writer.encrypt('test-password')
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
