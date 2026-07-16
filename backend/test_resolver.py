import sys
import logging
from app.services.reference_resolver import ReferenceResolver

logging.basicConfig(level=logging.INFO)

outline = {
    "indices": {
        "headings_by_name": {
            "executive summary": "paragraph_1",
            "company overview": "paragraph_9",
        }
    },
    "sections": [
        {
            "heading_id": "paragraph_1",
            "heading": "Executive Summary",
            "elements": [
                {"id": "paragraph_2", "type": "paragraph", "text_preview": "In Q3..."},
                {"id": "paragraph_3", "type": "paragraph", "text_preview": "Production downtime was reduced by 8%..."},
                {"id": "paragraph_4", "type": "paragraph", "text_preview": "The company signed a three-year supply agreement with Volt Energy Ltd..."},
                {"id": "paragraph_5", "type": "paragraph", "text_preview": "Customer research indicated..."},
            ]
        },
        {
            "heading_id": "paragraph_9",
            "heading": "Company Overview",
            "elements": [
                {"id": "paragraph_10", "type": "paragraph", "text_preview": "Apex Manufacturing Pvt. Ltd. specializes in..."},
            ]
        }
    ]
}

resolver = ReferenceResolver()
res = resolver.resolve("the company overview section", outline)
print(f"Result: {res}")
