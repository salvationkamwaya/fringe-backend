"""
PDF Report Generator for The Fringe Business System
Generates standards-compliant, publication-quality A4 PDF documents
with corporate styling, multi-page pagination, KPI metric cards, and data tables.
Zero external dependencies (pure Python).
"""

from datetime import datetime


class ReportPDFBuilder:
    def __init__(self, title, subtitle="", date_range_label="All Time", business_unit="Bar & Lounge"):
        self.title = title
        self.subtitle = subtitle
        self.date_range_label = date_range_label
        self.business_unit = business_unit
        
        self.width = 595.28   # A4 width in pt
        self.height = 841.89  # A4 height in pt
        self.margin_left = 40.0
        self.margin_right = 555.0
        self.content_width = self.margin_right - self.margin_left # 515 pt
        self.margin_bottom = 50.0
        
        self.pages = []
        self.current_page_stream = []
        self.cursor_y = self.height - 40.0
        self.page_number = 1

        # Start first page
        self._new_page(is_first=True)

    def _new_page(self, is_first=False):
        if not is_first:
            self._draw_footer()
            self.pages.append("".join(self.current_page_stream))
            self.current_page_stream = []
            self.page_number += 1

        self.cursor_y = self.height - 40.0
        if is_first:
            self._draw_document_header()
        else:
            self._draw_subsequent_page_header()

    def _draw_footer(self):
        # Footer divider line
        self.draw_line(self.margin_left, self.margin_bottom, self.margin_right, self.margin_bottom, (0.8, 0.85, 0.9), 0.75)
        # Footer text
        gen_time = datetime.now().strftime("%Y-%m-%d %H:%M")
        footer_text = f"The Fringe Enterprises (Tanzania)  |  Generated: {gen_time}  |  Currency: TSH"
        self.draw_text(footer_text, self.margin_left, self.margin_bottom - 14, size=8, color=(0.45, 0.5, 0.6))
        page_str = f"Page {self.page_number}"
        self.draw_text(page_str, self.margin_right - 45, self.margin_bottom - 14, size=8, bold=True, color=(0.45, 0.5, 0.6))

    def _draw_document_header(self):
        # Top banner background
        self.draw_rect(self.margin_left, self.cursor_y - 48, self.content_width, 52, fill_color=(0.06, 0.12, 0.22), stroke_color=(0.15, 0.25, 0.4))
        
        # Company & System tag
        self.draw_text("THE FRINGE BUSINESS SYSTEM  •  TANZANIA (TSH)", self.margin_left + 14, self.cursor_y - 14, size=8, bold=True, color=(0.95, 0.75, 0.2))
        # Main Title
        self.draw_text(self.title.upper(), self.margin_left + 14, self.cursor_y - 30, size=14, bold=True, color=(1.0, 1.0, 1.0))
        # Subtitle
        sub = f"Unit: {self.business_unit}  |  Period: {self.date_range_label}"
        self.draw_text(sub, self.margin_left + 14, self.cursor_y - 43, size=8.5, color=(0.75, 0.82, 0.9))

        self.cursor_y -= 62

    def _draw_subsequent_page_header(self):
        self.draw_line(self.margin_left, self.cursor_y, self.margin_right, self.cursor_y, (0.75, 0.8, 0.88), 0.75)
        header_text = f"{self.title}  •  {self.business_unit}  ({self.date_range_label})"
        self.draw_text(header_text, self.margin_left, self.cursor_y + 4, size=8, bold=True, color=(0.3, 0.4, 0.5))
        self.cursor_y -= 18

    def ensure_space(self, required_pt):
        if self.cursor_y - required_pt < self.margin_bottom + 10:
            self._new_page()

    def draw_text(self, text, x, y, size=10, bold=False, italic=False, color=(0.1, 0.1, 0.1)):
        font = "/F2" if bold else ("/F3" if italic else "/F1")
        r, g, b = color
        clean = str(text).replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        self.current_page_stream.append(
            f"BT {r:.2f} {g:.2f} {b:.2f} rg {font} {size} Tf {x:.2f} {y:.2f} Td ({clean}) Tj ET\n"
        )

    def draw_rect(self, x, y, w, h, fill_color=(0.95, 0.95, 0.95), stroke_color=None, stroke_width=0.75):
        r, g, b = fill_color
        cmd = f"{r:.2f} {g:.2f} {b:.2f} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f\n"
        if stroke_color:
            sr, sg, sb = stroke_color
            cmd += f"{sr:.2f} {sg:.2f} {sb:.2f} RG {stroke_width:.2f} w {x:.2f} {y:.2f} {w:.2f} {h:.2f} re s\n"
        self.current_page_stream.append(cmd)

    def draw_line(self, x1, y1, x2, y2, stroke_color=(0.8, 0.8, 0.8), width=1.0):
        r, g, b = stroke_color
        self.current_page_stream.append(
            f"{r:.2f} {g:.2f} {b:.2f} RG {width:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S\n"
        )

    def add_section_header(self, title):
        self.ensure_space(32)
        self.cursor_y -= 6
        self.draw_rect(self.margin_left, self.cursor_y - 16, self.content_width, 18, fill_color=(0.93, 0.95, 0.98), stroke_color=(0.75, 0.82, 0.92))
        self.draw_text(title.upper(), self.margin_left + 8, self.cursor_y - 12, size=9.5, bold=True, color=(0.1, 0.25, 0.5))
        self.cursor_y -= 24

    def add_kpi_grid(self, kpis):
        """
        kpis: list of dicts [{'label': 'Total Revenue', 'value': 'TSH 1,200.00', 'sub': '24 orders', 'color': (0.1, 0.4, 0.8)}]
        """
        count = len(kpis)
        if count == 0:
            return
        
        card_height = 42
        self.ensure_space(card_height + 10)
        gap = 8
        card_w = (self.content_width - (gap * (count - 1))) / count

        for i, kpi in enumerate(kpis):
            card_x = self.margin_left + i * (card_w + gap)
            card_y = self.cursor_y - card_height

            # Card background
            self.draw_rect(card_x, card_y, card_w, card_height, fill_color=(0.96, 0.97, 0.99), stroke_color=(0.82, 0.86, 0.92))
            
            # Label
            self.draw_text(kpi.get('label', '').upper(), card_x + 8, card_y + 30, size=7.5, bold=True, color=(0.4, 0.45, 0.55))
            # Value
            val_col = kpi.get('color', (0.05, 0.15, 0.35))
            self.draw_text(kpi.get('value', ''), card_x + 8, card_y + 16, size=11, bold=True, color=val_col)
            # Subtitle
            sub = kpi.get('sub', '')
            if sub:
                self.draw_text(sub, card_x + 8, card_y + 6, size=7, color=(0.5, 0.55, 0.6))

        self.cursor_y -= (card_height + 12)

    def add_table(self, headers, rows, col_widths, alignments=None):
        """
        headers: list of strings ['Product', 'Units', 'Revenue (TSH)']
        rows: list of lists
        col_widths: list of float widths (summing approx to 515)
        alignments: list of 'L' or 'R'
        """
        if not rows:
            self.ensure_space(24)
            self.draw_text("No records found for this reporting criteria.", self.margin_left + 10, self.cursor_y - 12, size=9, italic=True, color=(0.5, 0.5, 0.5))
            self.cursor_y -= 22
            return

        if not alignments:
            alignments = ['L'] * len(headers)

        header_height = 18
        row_height = 15

        def render_header():
            self.draw_rect(self.margin_left, self.cursor_y - header_height, self.content_width, header_height, fill_color=(0.15, 0.22, 0.32))
            cx = self.margin_left
            for idx, h in enumerate(headers):
                w = col_widths[idx]
                align = alignments[idx]
                tx = cx + 6 if align == 'L' else cx + w - 6 - (len(h) * 4.2)
                self.draw_text(h, max(cx + 4, tx), self.cursor_y - 12, size=8, bold=True, color=(1, 1, 1))
                cx += w
            self.cursor_y -= header_height

        self.ensure_space(header_height + row_height * 2)
        render_header()

        for r_idx, row in enumerate(rows):
            if self.cursor_y - row_height < self.margin_bottom + 10:
                self._new_page()
                render_header()

            bg_col = (0.97, 0.98, 1.0) if r_idx % 2 == 1 else (1.0, 1.0, 1.0)
            self.draw_rect(self.margin_left, self.cursor_y - row_height, self.content_width, row_height, fill_color=bg_col)
            self.draw_line(self.margin_left, self.cursor_y - row_height, self.margin_right, self.cursor_y - row_height, (0.9, 0.92, 0.95), 0.5)

            cx = self.margin_left
            for c_idx, cell in enumerate(row):
                w = col_widths[c_idx]
                align = alignments[c_idx]
                txt = str(cell)
                # Truncate if too long
                max_chars = max(4, int(w / 5.2))
                if len(txt) > max_chars:
                    txt = txt[:max_chars - 2] + ".."

                tx = cx + 6 if align == 'L' else cx + w - 6 - (len(txt) * 4.5)
                self.draw_text(txt, max(cx + 4, tx), self.cursor_y - 11, size=7.8, color=(0.15, 0.18, 0.25))
                cx += w

            self.cursor_y -= row_height

        self.cursor_y -= 8

    def build(self):
        # Finalize last page
        self._draw_footer()
        self.pages.append("".join(self.current_page_stream))

        page_count = len(self.pages)
        page_obj_ids = [3 + i for i in range(page_count)]
        content_obj_ids = [3 + page_count + i for i in range(page_count)]
        font1_id = 3 + 2 * page_count
        font2_id = font1_id + 1
        font3_id = font1_id + 2

        objects = []
        # Catalog
        objects.append(f"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
        # Pages
        kids_str = " ".join(f"{pid} 0 R" for pid in page_obj_ids)
        objects.append(f"2 0 obj\n<< /Type /Pages /Kids [{kids_str}] /Count {page_count} >>\nendobj\n")

        # Page objects
        res_dict = f"<< /Font << /F1 {font1_id} 0 R /F2 {font2_id} 0 R /F3 {font3_id} 0 R >> >>"
        for i in range(page_count):
            pid = page_obj_ids[i]
            cid = content_obj_ids[i]
            objects.append(
                f"{pid} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.width:.2f} {self.height:.2f}] "
                f"/Contents {cid} 0 R /Resources {res_dict} >>\nendobj\n"
            )

        # Content streams
        for i in range(page_count):
            cid = content_obj_ids[i]
            stream_data = self.pages[i].encode('latin-1', 'replace')
            objects.append(
                f"{cid} 0 obj\n<< /Length {len(stream_data)} >>\nstream\n{stream_data.decode('latin-1')}endstream\nendobj\n"
            )

        # Fonts (Standard 14 PDF fonts guaranteed in all PDF engines)
        objects.append(f"{font1_id} 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")
        objects.append(f"{font2_id} 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>\nendobj\n")
        objects.append(f"{font3_id} 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique >>\nendobj\n")

        # Construct file layout
        out = []
        out.append("%PDF-1.4\n")
        offsets = [0]
        curr_offset = len(out[0].encode('latin-1'))

        for obj in objects:
            offsets.append(curr_offset)
            encoded = obj.encode('latin-1')
            curr_offset += len(encoded)
            out.append(obj)

        startxref = curr_offset
        total_objs = len(objects) + 1
        out.append(f"xref\n0 {total_objs}\n")
        out.append("0000000000 65535 f \n")
        for off in offsets[1:]:
            out.append(f"{off:010d} 00000 n \n")

        out.append(f"trailer\n<< /Size {total_objs} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n")
        return "".join(out).encode('latin-1')

