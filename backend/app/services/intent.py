import json
import re

from app.core.config import settings


# Keywords that suggest the user wants to generate/populate a full deck
# rather than make targeted edits.
_GENERATE_PATTERNS = [
    r"\bcreate\b.*\b(ppt|presentation|deck|slides)\b",
    r"\bgenerate\b.*\b(ppt|presentation|deck|slides|content)\b",
    r"\bmake\b.*\b(ppt|presentation|deck)\b.*\babout\b",
    r"\bfill\b.*\b(template|slides|blank)\b",
    r"\bpopulate\b.*\b(template|slides)\b",
    r"\bbuild\b.*\b(ppt|presentation|deck)\b",
    r"\bprepare\b.*\b(ppt|presentation|deck)\b",
    r"\b(ppt|presentation|deck)\b.*\bon\b",
    r"\badd\s+(\d+\s+)?(more\s+)?slides?\b",
    r"\bdelete\s+(slide|slides)\b",
    r"\bremove\s+(slide|slides)\b",
]

# ---- Operation-category keyword patterns --------------------------------

_FORMAT_PATTERNS = [
    r"\b(bold|italic|underline|strikethrough)\b",
    r"\bfont\s+(family|size|color|face|style)\b",
    r"\bchange\s+(the\s+)?(font|size|color|alignment|spacing)\b",
    r"\b(center|left|right|justify)\s+(align|aligned|alignment)\b",
    r"\balign\s+(to\s+)?(center|left|right)\b",
    r"\b(highlight|color)\s+(the\s+)?(text|title|heading|paragraph)\b",
    r"\bfont\s+size\b",
    r"\b\d{1,3}pt\b",
    r"\b(line|paragraph|character)\s+spacing\b",
    r"\b(superscript|subscript)\b",
    r"\bshadow\b.*\btext\b",
    r"\btext\s+effect\b",
    r"\bglow\b",
    r"\breflection\b",
    r"\bindent\b",
    r"\bbullet(s|ed)?\b",
    r"\bnumber(ed|ing)?\s+list\b",
    r"\btext\s+direction\b",
    r"\bmake\s+(it\s+)?(bold|italic|larger|smaller|bigger)\b",
    r"\b(increase|decrease|change)\s+(the\s+)?font\b",
    r"\bset\s+(the\s+)?(font|size|color)\b",
    r"\b(text|font)\s+color\b",
    # Color-change patterns (the most commonly missed)
    r"\bchange\s+.{0,60}\bcolor\b",
    r"\bcolor\s+.{0,60}\bto\b",
    r"\bto\s+(red|blue|green|yellow|orange|purple|pink|black|white|gray|grey|cyan|magenta|teal|navy|maroon|gold|silver|brown|violet|indigo)\b",
    r"\bmake\s+.{0,60}\b(red|blue|green|yellow|orange|purple|pink|black|white|gray|grey|cyan|magenta|teal|navy|maroon|gold|silver|brown|violet|indigo)\b",
    r"\b(red|blue|green|yellow|orange|purple|pink|black|white|gray|grey|cyan|magenta|teal|navy|maroon|gold|silver|brown|violet|indigo)\s+(color|font|text|background|fill|heading|title|subtitle|highlight)\b",
    r"\bcolor\s+(the\s+)?(heading|title|subtitle|text|paragraph|subheading|header)\b",
    r"\bchange\s+(the\s+)?(heading|title|subtitle|subheading|header|text|paragraph)\s+(color|colour)\b",
    r"\b(colour|color)\s+scheme\b",
    r"\bhex\s+#?[0-9a-fA-F]{3,6}\b",
    r"#[0-9a-fA-F]{3,6}\b",
    # Broad catch-all for formatting phrases
    r"\bchange\s+.{0,60}\bto\s+.{0,30}\b(bold|italic|underline|pt)\b",
    r"\bhighlight\b.{0,60}\b(yellow|green|blue|red|pink)\b",
    r"\b(change|make)\s+.{0,60}\b(1[0-9]|2[0-9]|[6-9])pt\b",
]

_TABLE_PATTERNS = [
    r"\b(create|add|insert|make)\s+a\s+table\b",
    r"\btable\s+(with|of)\b",
    r"\b(add|insert|remove|delete)\s+(a\s+)?(row|column|col)\b",
    r"\bmerge\s+(cells?|columns?|rows?)\b",
    r"\bsplit\s+cells?\b",
    r"\btable\s+(style|border|color|background)\b",
    r"\bcell\s+(background|padding|alignment|color)\b",
    r"\balternate\s+row\s+color\b",
    r"\bheader\s+row\b",
    r"\bpopulate\s+(the\s+)?table\b",
    r"\bformat\s+(the\s+)?table\b",
    r"\bsort\s+(the\s+)?table\b",
    r"\bdelete\s+(the\s+)?table\b",
    r"\bremove\s+(the\s+)?table\b",
    r"\bresize\s+(rows?|columns?|cells?)\b",
]

_IMAGE_PATTERNS = [
    r"\binsert\s+(an?\s+)?(image|photo|picture|graphic|logo)\b",
    r"\badd\s+(an?\s+)?(image|photo|picture|graphic|logo)\b",
    r"\b(replace|swap)\s+(the\s+)?(image|photo|picture|graphic)\b",
    r"\b(remove|delete)\s+(the\s+)?(image|photo|picture|graphic)\b",
    r"\b(resize|scale|crop|rotate)\s+(the\s+)?(image|photo|picture)\b",
    r"\b(image|picture|photo)\s+(border|shadow|transparency|opacity)\b",
    r"\brounded\s+corners\b",
    r"\bbring\s+(forward|to front)\b",
    r"\bsend\s+(backward|to back)\b",
    r"\b(move|reposition)\s+(the\s+)?(image|photo|picture)\b",
    r"\baspect\s+ratio\b",
    r"\bplace(holder)?\s+(image|photo)\b",
    r"\bpicture\s+placeholder\b",
]

_SHAPE_PATTERNS = [
    r"\badd\s+(a\s+)?text\s*box\b",
    r"\binsert\s+(a\s+)?text\s*box\b",
    r"\b(move|resize|rotate|duplicate)\s+(the\s+)?(shape|text\s*box|object|box)\b",
    r"\b(delete|remove)\s+(the\s+)?(shape|text\s*box|object|box)\b",
    r"\bgroup\s+(objects?|shapes?|elements?)\b",
    r"\bungroup\b",
    r"\bfill\s+(color|colour)\b",
    r"\boutline\s+(color|colour|thickness|width)\b",
    r"\bshape\s+(fill|color|style|outline)\b",
    r"\b(align|distribute)\s+(objects?|shapes?|elements?)\b",
    r"\blayer(ing)?\b",
    r"\bz-?order\b",
]

_THEME_PATTERNS = [
    r"\b(change|set|update)\s+(the\s+)?background\b",
    r"\bbackground\s+(color|colour|image|gradient|pattern)\b",
    r"\bapply\s+(a\s+)?(theme|branding|color\s+palette)\b",
    r"\b(corporate|brand)\s+(branding|colors?|colors?|theme)\b",
    r"\bcolor\s+palette\b",
    r"\b(dark|light)\s+(mode|theme|background)\b",
    r"\bgradient\s+background\b",
    r"\bpattern\s+background\b",
    r"\baccent\s+color\b",
    r"\bprimary\s+color\b",
    r"\bslide\s+background\b",
    r"\b(update|change)\s+(the\s+)?theme\b",
]

_SLIDE_OP_PATTERNS = [
    r"\badd\s+(a\s+)?(new\s+)?slide\b",
    r"\binsert\s+(a\s+)?(new\s+)?slide\b",
    r"\bduplicate\s+(slide|this\s+slide)\b",
    r"\bdelete\s+(this\s+)?slide\b",
    r"\bremove\s+(this\s+)?slide\b",
    r"\breorder\s+slide\b",
    r"\bmove\s+slide\b",
    r"\bhide\s+slide\b",
    r"\bunhide\s+slide\b",
    r"\brename\s+(slide|the\s+slide)\b",
    r"\bapply\s+layout\b",
    r"\bchange\s+(the\s+)?layout\b",
]

_AI_DESIGN_PATTERNS = [
    r"\bmake\s+(it|this|the\s+slide|the\s+deck)?\s*more\s+professional\b",
    r"\bimprove\s+(the\s+)?(design|layout|visual|look|appearance)\b",
    r"\bbetter\s+(design|layout|look)\b",
    r"\bvisual\s+(hierarchy|consistency|polish)\b",
    r"\bnormalize\s+(fonts?|spacing|formatting)\b",
    r"\bmake\s+(fonts?|spacing|formatting)\s+consistent\b",
    r"\bbalance\s+whitespace\b",
    r"\bauto\s*(fix|clean|layout)\b",
    r"\bremove\s+overlap(s|ping)?\b",
    r"\bauto\s*resize\s+text\b",
    r"\bgenerate\s+speaker\s+notes\b",
    r"\bconvert\s+bullet(s)?\s+to\s+(diagram|infographic)\b",
    r"\bdetect\s+(clutter|inconsistencies)\b",
    r"\bimprove\s+readability\b",
    r"\bmake\s+(all\s+)?slides?\s+(consistent|uniform)\b",
    r"\bmatch\s+(the\s+)?(brand|branding|theme)\b",
]

_CHART_PATTERNS = [
    r"\b(create|add|insert)\s+(a\s+)?(chart|graph)\b",
    r"\bchange\s+(the\s+)?chart\s+type\b",
    r"\bupdate\s+(the\s+)?(chart|graph)\s+data\b",
    r"\b(chart|graph)\s+(legend|axis|label|color|style|theme)\b",
    r"\bdata\s+label\b",
    r"\bseries\s+color\b",
    r"\bbar\s+chart\b",
    r"\bpie\s+chart\b",
    r"\bline\s+(graph|chart)\b",
    r"\bcolumn\s+chart\b",
    r"\bscatter\s+(plot|chart)\b",
]

_LAYOUT_PATTERNS = [
    r"\bmove\s+(the\s+)?(section|block|paragraph|heading)\b",
    r"\binsert\s+(a\s+)?page\s*break\b",
    r"\b(page\s*break|section\s*break)\b",
    r"\breorder\s+(the\s+)?(sections|paragraphs)\b",
]

_LIST_PATTERNS = [
    r"\bconvert\b.*\b(list|bullet|numbered|checklist)\b",
    r"\b(bullet|numbered|checklist)\b",
    r"\bsort\b.*\b(list|bullet|item)\b",
    r"\badd\b.*\b(item|highlight)\b",
    r"\bbullet\s*point\b",
]

_FIND_REPLACE_PATTERNS = [
    r"\b(find|replace)\b",
    r"\b(change|replace)\b.*\b(every|all)\b.*\b(occurrence|instance)s?\b",
    r"\bglobal(ly)?\s+replace\b",
    r"\b(change|update|replace)\b.*\bplaceholder\b",
]

def _matches(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


class IntentClassifier:
    """Classifies the user's editing intent.

    Returns a dict with:
    - ``mode``         : "edit" | "generate" | "operations"
    - ``op_category``  : for operations mode — which kind of op is needed
    - ``topic``        : for generate mode
    - ``slide_count``  : int | None
    - ``delete_slides``: list[int]
    - ``add_slides_count`` : int | None
    - ``direct_target``: bool
    - ``semantic_search_required``: bool
    - ``semantic_query``: str
    - ``slide``        : int | None
    - ``paragraph``    : int | None
    """

    def classify(self, request: str, chat_history: list[dict] | None = None) -> dict:
        if settings.openai_api_key:
            try:
                return self._classify_with_llm(request, chat_history or [])
            except Exception:
                pass
        return self._classify_local(request)

    # ------------------------------------------------------------------
    # LLM path
    # ------------------------------------------------------------------

    def _classify_with_llm(self, request: str, chat_history: list[dict]) -> dict:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )
        system_prompt = (
            "You are an assistant that classifies document editing requests.\n"
            "Given a user instruction and conversation history, return a JSON object.\n\n"
            "CRITICAL RULES (must follow exactly):\n"
            "1. If the request involves ANY of: colors, bold, italic, underline, font size, "
            "font family, alignment, spacing, background color, text color, highlighting — "
            "set mode='operations' and op_category='text_format'. "
            "These CANNOT be done in edit mode.\n"
            "2. If the request involves creating/inserting/deleting tables, rows, columns — "
            "mode='operations', op_category='table_op'.\n"
            "3. If the request involves inserting/removing/resizing images or logos — "
            "mode='operations', op_category='image_op'.\n"
            "4. If the request involves backgrounds, themes, color palettes — "
            "mode='operations', op_category='theme_op'.\n"
            "5. If the request involves adding/deleting/duplicating slides — "
            "mode='operations', op_category='slide_op'.\n"
            "6. If the request involves charts — mode='operations', op_category='chart_op'.\n"
            "7. If the request involves structural layout changes (moving sections, page breaks) — "
            "mode='operations', op_category='layout_op'.\n"
            "8. If the request involves lists (converting formats, adding items, sorting) — "
            "mode='operations', op_category='list_op'.\n"
            "9. If the request involves document-wide find and replace (replace all occurrences of X with Y) — "
            "mode='operations', op_category='find_replace'.\n"
            "10. If the request involves 'make professional', 'improve design', 'normalize fonts', "
            "'generate speaker notes' — mode='operations', op_category='ai_design_op'.\n"
            "11. If generating/creating a full new presentation or populating a deck — mode='generate'.\n"
            "12. Only use mode='edit' for pure TEXT CONTENT rewrites (rewriting sentences/paragraphs, "
            "adding/removing text content) with NO formatting changes.\n\n"
            "Return JSON with:\n"
            "  - mode (str): 'generate' | 'operations' | 'edit'\n"
            "  - op_category (str): when mode='operations', one of: "
            "'text_format'|'table_op'|'image_op'|'shape_op'|'theme_op'|'slide_op'|'chart_op'|'layout_op'|'list_op'|'find_replace'|'ai_design_op'. "
            "Empty string for other modes.\n"
            "  - topic (str): if mode='generate', the subject. Otherwise ''.\n"
            "  - slide_count (int|null): number of slides requested. null if not specified.\n"
            "  - delete_slides (list[int]): 1-based slide numbers to delete. [] if none.\n"
            "  - add_slides_count (int|null): number of slides to add. null if not specified.\n"
            "  - direct_target (bool): true if user explicitly targets a specific slide or paragraph number.\n"
            "  - semantic_search_required (bool): true if target must be found by content search.\n"
            "  - semantic_query (str): search query for the target content. If follow-up, infer from history.\n"
            "  - slide (int|null): 1-based slide number if mentioned. null otherwise.\n"
            "  - paragraph (int|null): 1-based paragraph number if mentioned. null otherwise.\n"
            "Return ONLY valid JSON with no commentary."
        )

        history_str = ""
        if chat_history:
            history_str = "Previous conversation history:\n"
            for msg in chat_history[-5:]:
                role = "User" if msg["role"] == "user" else "Agent"
                history_str += f"{role}: {msg['content']}\n"
            history_str += "\n"

        user_prompt = f"{history_str}User Instruction: {request}"

        response = client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        raw = (response.choices[0].message.content or "{}").strip()
        data = json.loads(raw)

        mode = data.get("mode", "edit")
        if mode not in ("edit", "generate", "operations"):
            mode = "edit"

        return {
            "mode": mode,
            "op_category": str(data.get("op_category", "")),
            "topic": str(data.get("topic", "")),
            "slide_count": data.get("slide_count") if isinstance(data.get("slide_count"), int) else None,
            "delete_slides": [s for s in data.get("delete_slides", []) if isinstance(s, int)],
            "add_slides_count": data.get("add_slides_count") if isinstance(data.get("add_slides_count"), int) else None,
            "direct_target": bool(data.get("direct_target", False)),
            "semantic_search_required": bool(data.get("semantic_search_required", True)),
            "semantic_query": data.get("semantic_query") or request,
            "slide": data.get("slide") if isinstance(data.get("slide"), int) else None,
            "paragraph": data.get("paragraph") if isinstance(data.get("paragraph"), int) else None,
        }

    # ------------------------------------------------------------------
    # Local fallback
    # ------------------------------------------------------------------

    def _classify_local(self, request: str) -> dict:
        lowered = request.lower()

        # Check for generation patterns first
        is_generate = any(re.search(p, lowered) for p in _GENERATE_PATTERNS)

        # Check for operations-mode patterns
        op_category = ""
        if not is_generate:
            if _matches(_FORMAT_PATTERNS, request):
                op_category = "text_format"
            elif _matches(_TABLE_PATTERNS, request):
                op_category = "table_op"
            elif _matches(_IMAGE_PATTERNS, request):
                op_category = "image_op"
            elif _matches(_SHAPE_PATTERNS, request):
                op_category = "shape_op"
            elif _matches(_THEME_PATTERNS, request):
                op_category = "theme_op"
            elif _matches(_SLIDE_OP_PATTERNS, request):
                op_category = "slide_op"
            elif _matches(_CHART_PATTERNS, request):
                op_category = "chart_op"
            elif _matches(_LAYOUT_PATTERNS, request):
                op_category = "layout_op"
            elif _matches(_LIST_PATTERNS, request):
                op_category = "list_op"
            elif _matches(_FIND_REPLACE_PATTERNS, request):
                op_category = "find_replace"
            elif _matches(_AI_DESIGN_PATTERNS, request):
                op_category = "ai_design_op"

        is_operations = bool(op_category) and not is_generate
        mode = "generate" if is_generate else ("operations" if is_operations else "edit")

        # Extract topic (rough heuristic)
        topic = ""
        topic_match = re.search(
            r"\b(?:on|about|for|regarding)\s+(.+?)(?:\.|$)", request, flags=re.IGNORECASE
        )
        if topic_match:
            topic = topic_match.group(1).strip()

        # Extract slide count
        slide_count = None
        sc_match = re.search(r"(\d+)\s*(?:slide|page)", lowered)
        if sc_match:
            slide_count = int(sc_match.group(1))

        # Extract delete slide numbers
        delete_slides: list[int] = []
        del_match = re.search(r"(?:delete|remove)\s+slides?\s+([\d,\s]+)", lowered)
        if del_match:
            delete_slides = [int(n.strip()) for n in del_match.group(1).split(",") if n.strip().isdigit()]

        # Extract add slide count
        add_slides_count = None
        add_match = re.search(r"add\s+(\d+)\s+(?:more\s+)?slides?", lowered)
        if add_match:
            add_slides_count = int(add_match.group(1))

        slide = re.search(r"\bslide\s+(\d+)\b", request, flags=re.IGNORECASE)
        paragraph = re.search(r"\bparagraph\s+(\d+)\b", request, flags=re.IGNORECASE)

        return {
            "mode": mode,
            "op_category": op_category,
            "topic": topic if is_generate else "",
            "slide_count": slide_count,
            "delete_slides": delete_slides,
            "add_slides_count": add_slides_count,
            "direct_target": bool(slide or paragraph) and not is_generate,
            "semantic_search_required": not bool(slide or paragraph) and not is_generate,
            "semantic_query": topic or request,
            "slide": int(slide.group(1)) if slide else None,
            "paragraph": int(paragraph.group(1)) if paragraph else None,
        }
