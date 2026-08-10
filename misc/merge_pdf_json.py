import json
import fitz   # PyMuPDF


PDF_INPUT = "scan.pdf"
JSON_INPUT = "ocr.json"
PDF_OUTPUT = "searchable.pdf"


# 中文字体
FONT_FILE = r"C:\Windows\Fonts\msyh.ttc"


def ocr_box_to_pdf_rect(box, page):

    """
    PaddleOCR:
        原点左上

    PDF:
        原点左上 (PyMuPDF也是)

    所以无需翻转Y
    """

    x1, y1, x2, y2 = box


    return fitz.Rect(
        x1,
        y1,
        x2,
        y2
    )


def estimate_font_size(rect):

    """
    根据bbox高度估算字号
    """

    h = rect.height

    return max(
        4,
        h * 0.8
    )


def add_text_layer(
        pdf_path,
        json_path,
        output):


    doc = fitz.open(pdf_path)


    with open(
        json_path,
        "r",
        encoding="utf-8"
    ) as f:
        data=json.load(f)


    # PaddleOCR:
    # [
    #   {
    #     "prunedResult": {...}
    #   }
    # ]

    pages=data


    for page_id,page_data in enumerate(pages):

        if page_id >= len(doc):
            break


        pdf_page=doc[page_id]


        result=page_data["prunedResult"]


        texts=result["rec_texts"]

        boxes=result["rec_boxes"]


        for text,box in zip(
            texts,
            boxes
        ):

            if not text.strip():
                continue


            x1,y1,x2,y2=box


            rect=fitz.Rect(
                x1,
                y1,
                x2,
                y2
            )


            fontsize=max(
                5,
                rect.height*0.8
            )


            pdf_page.insert_textbox(
                rect,
                text,
                fontfile=FONT_FILE,
                fontsize=fontsize,
                render_mode=3
            )


    doc.save(
        output,
        garbage=4,
        deflate=True
    )

    doc.close()

if __name__=="__main__":

    add_text_layer(
        PDF_INPUT,
        JSON_INPUT,
        PDF_OUTPUT
    )

    print(
        "完成:",
        PDF_OUTPUT
    )