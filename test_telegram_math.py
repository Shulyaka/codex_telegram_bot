import unittest

from telegramify_markdown import telegramify_rich

from telegram_math import normalize_math_delimiters


class TelegramMathTests(unittest.TestCase):
    @staticmethod
    def rendered(markdown: str) -> str:
        return "".join(item.to_dict()["html"] for item in telegramify_rich(normalize_math_delimiters(markdown)))

    def test_inline_formula_preserves_tex_and_renders_natively(self) -> None:
        source = r"Potential \(\varphi\)."
        self.assertEqual(normalize_math_delimiters(source), r"Potential $\varphi$.")
        self.assertEqual(self.rendered(source), r"<p>Potential <tg-math>\varphi</tg-math>.</p>")

    def test_inline_boundary_whitespace_is_trimmed_without_changing_tex(self) -> None:
        source = r"\(  \frac{a}{b} + x  \)"
        self.assertEqual(normalize_math_delimiters(source), r"$\frac{a}{b} + x$")
        self.assertIn(r"<tg-math>\frac{a}{b} + x</tg-math>", self.rendered(source))

    def test_display_formula_preserves_tex_and_renders_natively(self) -> None:
        source = r"\[\mathbf E=-\nabla\varphi.\]"
        self.assertEqual(normalize_math_delimiters(source), r"$$\mathbf E=-\nabla\varphi.$$")
        self.assertEqual(self.rendered(source), r"<tg-math-block>\mathbf E=-\nabla\varphi.</tg-math-block>")

    def test_multiline_aligned_display_keeps_row_breaks_and_commands(self) -> None:
        body = "\n" + r"\begin{aligned}" + "\n" + r"a&=b\\" + "\n" + r"c&=d" + "\n" + r"\end{aligned}" + "\n"
        source = "\\[" + body + "\\]"
        self.assertEqual(normalize_math_delimiters(source), "$$" + body + "$$")
        html = self.rendered(source)
        self.assertIn("<tg-math-block>", html)
        self.assertIn(r"\begin{aligned}", html)
        self.assertIn(r"a&amp;=b\\", html)
        self.assertIn(r"c&amp;=d", html)

    def test_indented_tex_inside_display_is_not_mistaken_for_markdown_code(self) -> None:
        source = "\\[\n    \\mathbf E = -\\nabla \\varphi\n\\]"
        self.assertIn("<tg-math-block>", self.rendered(source))
        self.assertIn(r"\mathbf E = -\nabla \varphi", self.rendered(source))

    def test_same_line_display_keeps_surrounding_text(self) -> None:
        source = r"before\[x\]after"
        self.assertEqual(normalize_math_delimiters(source), "before$$x$$after")
        self.assertIn("before", self.rendered(source))
        self.assertIn("<tg-math-block>x</tg-math-block>", self.rendered(source))
        self.assertIn("after", self.rendered(source))

    def test_adjacent_punctuation_and_tex_parentheses_are_preserved(self) -> None:
        source = r"(\(\varphi\)), and \(\left(\frac{a}{b}\right)\)."
        self.assertEqual(normalize_math_delimiters(source), r"($\varphi$), and $\left(\frac{a}{b}\right)$.")
        self.assertEqual(self.rendered(source).count("<tg-math>"), 2)

    def test_inline_code_is_unchanged_while_adjacent_math_is_normalized(self) -> None:
        source = r"Example `\(x\)` and \(y\)."
        self.assertEqual(normalize_math_delimiters(source), r"Example `\(x\)` and $y$.")
        self.assertIn(r"<code>\(x\)</code>", self.rendered(source))

    def test_variable_length_backtick_spans_preserve_embedded_backticks(self) -> None:
        source = r"``example ` \(x\) ` literal`` and \(y\)"
        self.assertEqual(normalize_math_delimiters(source), r"``example ` \(x\) ` literal`` and $y$")

    def test_fenced_code_and_existing_math_fences_remain_unchanged(self) -> None:
        for marker in ("```", "````", "~~~", "~~~~"):
            with self.subTest(marker=marker):
                source = f"{marker}latex\n" + r"\(x\) \[y\]" + f"\n{marker}\n" + r"\(z\)"
                expected = source[:-5] + "$z$"
                self.assertEqual(normalize_math_delimiters(source), expected)
        existing_math = "```math\n\\mathbf E=-\\nabla\\varphi\n```"
        self.assertEqual(normalize_math_delimiters(existing_math), existing_math)
        self.assertIn("<tg-math-block>", self.rendered(existing_math))

    def test_longer_fences_can_contain_shorter_fences(self) -> None:
        for outer, inner in (("````", "```"), ("~~~~", "~~~")):
            with self.subTest(outer=outer):
                code = f"{outer}\n{inner}\n" + r"\(literal\)" + f"\n{inner}\n{outer}\n"
                self.assertEqual(normalize_math_delimiters(code + r"\(x\)"), code + "$x$")

    def test_unclosed_fence_keeps_the_rest_literal(self) -> None:
        for marker in ("```", "~~~"):
            with self.subTest(marker=marker):
                source = f"{marker}latex\n" + r"\(x\)"
                self.assertEqual(normalize_math_delimiters(source), source)

    def test_quoted_and_list_fenced_code_stays_literal(self) -> None:
        sources = (
            "> ```latex\n> \\(x\\)\n> ```\n",
            "> ~~~~latex\n> \\[x\\]\n> ~~~~\n",
            "- ~~~latex\n  \\(x\\)\n  ~~~\n",
        )
        for code in sources:
            with self.subTest(code=code):
                self.assertEqual(normalize_math_delimiters(code + r"\(y\)"), code + "$y$")

    def test_indented_code_stays_literal(self) -> None:
        source = "    \\(x\\)\n\t\\[y\\]\n" + r"\(z\)"
        self.assertEqual(normalize_math_delimiters(source), "    \\(x\\)\n\t\\[y\\]\n$z$")

    def test_escaped_delimiters_stay_literal(self) -> None:
        for source in (r"\\(literal\\)", r"\\[literal\\]", r"\\\(literal\\\)"):
            with self.subTest(source=source):
                self.assertEqual(normalize_math_delimiters(source), source)

    def test_unmatched_mixed_and_empty_delimiters_stay_literal(self) -> None:
        for source in (r"\(x", r"\[x", r"x\)", r"\(x\]", r"\[x\)", r"\(\)", r"\[  \]"):
            with self.subTest(source=source):
                self.assertEqual(normalize_math_delimiters(source), source)

    def test_multiline_inline_formula_stays_literal_including_tex_comments(self) -> None:
        source = "\\(x % comment\n+ y\\)"
        self.assertEqual(normalize_math_delimiters(source), source)

    def test_existing_dollar_math_is_not_rewritten_inside(self) -> None:
        for source in (r"$\text{\(literal\)}$", r"$$\text{\[literal\]}$$", r"$\varphi$ and $$\mathbf E=-\nabla\varphi$$"):
            with self.subTest(source=source):
                self.assertEqual(normalize_math_delimiters(source), source)

    def test_currency_does_not_swallow_math_between_prices(self) -> None:
        source = r"Cost $5 with \(x\), then $10."
        self.assertEqual(normalize_math_delimiters(source), "Cost $5 with $x$, then $10.")
        self.assertIn("<tg-math>x</tg-math>", self.rendered(source))

    def test_link_destinations_are_preserved_but_labels_can_contain_math(self) -> None:
        source = r"[\(x\)](https://example.test/\(backup\)) and \(y\)"
        self.assertEqual(normalize_math_delimiters(source), r"[$x$](https://example.test/\(backup\)) and $y$")
        image = r"![file](folder/\(backup\).png)"
        self.assertEqual(normalize_math_delimiters(image), image)
        autolink = r"<https://example.test/\(backup\)>"
        self.assertEqual(normalize_math_delimiters(autolink), autolink)

    def test_quoted_and_list_display_formulas_keep_container_markup(self) -> None:
        for source in ("> \\[\n> x\n> \\]", "- \\[\n  x\n  \\]"):
            with self.subTest(source=source):
                self.assertIn("<tg-math-block>", self.rendered(source))
                self.assertIn("x", self.rendered(source))


if __name__ == "__main__":
    unittest.main()
