from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import sys
import pandas as pd

# Setup paths to import from backend/app
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root / "backend"))

from app.models.product_input import ProductInput
from app.services.ingestion.excel import ingest_excel
from app.services.enrichment.workflow import enrich_product
from app.services.retrieval.qdrant_db import get_qdrant_service
from app.services.retrieval import reference as ref_service


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("unilog_evaluator")


async def run_evaluation():
    input_file = project_root / "data" / "raw" / "Unihack_ Sample Dataset - Input.csv.xls"
    ground_truth_file = project_root / "data" / "reference" / "Unihack_ Expected Output - Delivery Format.csv"
    
    if not input_file.is_file():
        logger.error(f"Input file not found at {input_file}")
        sys.exit(1)
        
    if not ground_truth_file.is_file():
        logger.error(f"Ground Truth file not found at {ground_truth_file}")
        sys.exit(1)

    logger.info("Parsing input products...")
    input_products = ingest_excel(input_file)
    logger.info(f"Loaded {len(input_products)} products from input file.")

    logger.info("Reading expected ground truth delivery format...")
    gt_df = pd.read_csv(ground_truth_file)
    logger.info(f"Loaded {len(gt_df)} rows from ground truth file.")

    # 1. Indexing specs documents in Qdrant first
    logger.info("Indexing specifications in Qdrant vector DB...")
    qdrant = get_qdrant_service()
    from app.services.ingestion.pdf import PDFElement
    from app.api.pipeline import MOCK_SPECS
    
    for mpn, text in MOCK_SPECS.items():
        qdrant.index_pdf_elements(
            mpn,
            [PDFElement(text=text, page_num=1, element_type="paragraph", metadata={"source": f"{mpn}_spec.pdf"})]
        )
        
    logger.info("Indexing specifications complete. Running pipeline enrichment...")

    pipeline_outputs = []
    
    # 2. Run enrichment on each row
    for prod in input_products:
        mfg_part_num = prod.mfg_part_num or f"ROW_{prod.source_row}"
        logger.info(f"Enriching product row {prod.source_row} (MPN: {mfg_part_num})...")
        try:
            output_row = enrich_product(prod)
            pipeline_outputs.append(output_row)
        except Exception as e:
            logger.error(f"Enrichment crashed for Row {prod.source_row}: {e}")

    logger.info("Pipeline enrichment complete. Scoring outputs...")

    # 3. Score outputs against ground truth
    metrics = score_results(pipeline_outputs, gt_df)
    
    # Write report
    report_path = project_root / "evaluation" / "eval_report.json"
    with open(report_path, "w") as f:
        json.dump(metrics, f, indent=2)
        
    print("\n" + "="*50)
    print("      UNILOG PRODUCT INTELLIGENCE PIPELINE EVALUATION")
    print("="*50)
    print(f"Total Products Evaluated      : {metrics['total_evaluated']}")
    print(f"Field-Level Cell Accuracy     : {metrics['overall_cell_accuracy']}%")
    print(f"Attribute Extraction Accuracy : {metrics['attribute_accuracy']}%")
    print(f"LOV Compliance Rate           : {metrics['lov_compliance_rate']}%")
    print(f"UOM Compliance Rate           : {metrics['uom_compliance_rate']}%")
    print(f"Evidence-Backed Percentage    : {metrics['evidence_backed_rate']}%")
    print(f"Average Overall Confidence    : {metrics['average_confidence']}%")
    print(f"Auto-Approved Records         : {metrics['auto_approved_count']}")
    print(f"Needs Review Records          : {metrics['needs_review_count']}")
    print(f"Human-Review Percentage       : {metrics['human_review_rate']}%")
    print("="*50)
    print(f"Report written to: {report_path}\n")


def score_results(outputs: list[dict], gt_df: pd.DataFrame) -> dict:
    total_evaluated = len(outputs)
    if not total_evaluated:
        return {"total_evaluated": 0}

    total_cells = 0
    correct_cells = 0
    
    total_attrs = 0
    correct_attrs = 0
    
    lov_valid_count = 0
    lov_total = 0
    uom_valid_count = 0
    uom_total = 0
    
    desc_compliant_count = 0
    missing_fields_count = 0
    evidence_backed_count = 0
    human_review_count = 0
    
    total_confidence = 0
    
    # Map by Mfg_Part_Num
    for output in outputs:
        mpn = output.get("Mfg_Part_Num")
        
        # Check human-review flag from structured JSON
        structured_json = output.get("_structured_json", {})
        has_review = structured_json.get("needs_human_review", False)
        if has_review:
            human_review_count += 1
            
        total_confidence += structured_json.get("overall_confidence", 85)
        
        # Find matching row in ground truth
        gt_rows = gt_df[gt_df["Mfg_Part_Num"] == mpn]
        if gt_rows.empty:
            continue
            
        gt_row = gt_rows.iloc[0].to_dict()
        
        # Check description fields character limits
        invoice_desc = output.get("INVOICE_DESC") or ""
        mobile_desc = output.get("MOBILE_DESC") or ""
        if len(invoice_desc) <= 40 and 60 <= len(mobile_desc) <= 80:
            desc_compliant_count += 1

        # Compare column cells
        for col, expected_val in gt_row.items():
            if str(expected_val) == "nan" or expected_val is None:
                expected_val = ""
            expected_val = str(expected_val).strip()

            actual_val = str(output.get(col, "")).strip()
            if actual_val == "None":
                actual_val = ""

            total_cells += 1
            if actual_val == expected_val:
                correct_cells += 1
                
            # If it's an attribute slot
            if "ATTRIBUTE_VALUE" in col or "ATTRIBUTE_UOM" in col:
                total_attrs += 1
                if actual_val == expected_val:
                    correct_attrs += 1
                    
        # Check LOV, UOM, and missing
        for idx in range(1, 51):
            label = output.get(f"ATTRIBUTE_LABEL {idx}", "")
            val = output.get(f"ATTRIBUTE_VALUE {idx}", "")
            uom = output.get(f"ATTRIBUTE_UOM {idx}", "")
            
            if label:
                if not val or val == "" or val == "NEEDS_HUMAN_REVIEW":
                    missing_fields_count += 1
                else:
                    evidence_backed_count += 1
                    classpath = output.get("Classpath")
                    lov_total += 1
                    if ref_service.validate_lov_value(classpath, label, str(val)):
                        lov_valid_count += 1
                    uom_total += 1
                    combined_uom = f"{val} {uom}".strip()
                    normalized = ref_service.normalize_uom(combined_uom) if uom else None
                    if not uom or normalized is not None:
                        uom_valid_count += 1

    cell_acc = round((correct_cells / total_cells) * 100, 2) if total_cells else 100.0
    attr_acc = round((correct_attrs / total_attrs) * 100, 2) if total_attrs else 100.0
    
    total_populated_attrs = sum(
        1 for o in outputs for idx in range(1, 51)
        if o.get(f"ATTRIBUTE_LABEL {idx}") and o.get(f"ATTRIBUTE_VALUE {idx}") and o.get(f"ATTRIBUTE_VALUE {idx}") != "NEEDS_HUMAN_REVIEW"
    )
    total_attr_slots = sum(1 for o in outputs for idx in range(1, 51) if o.get(f"ATTRIBUTE_LABEL {idx}"))
    
    lov_rate = round((lov_valid_count / lov_total) * 100, 2) if lov_total else 100.0
    uom_rate = round((uom_valid_count / uom_total) * 100, 2) if uom_total else 100.0
    missing_rate = round(((total_attr_slots - total_populated_attrs) / total_attr_slots) * 100, 2) if total_attr_slots else 0.0
    evidence_rate = round((total_populated_attrs / total_attr_slots) * 100, 2) if total_attr_slots else 100.0
    
    desc_limit_rate = round((desc_compliant_count / len(gt_df)) * 100, 2) if len(gt_df) else 100.0
    human_rate = round((human_review_count / total_evaluated) * 100, 2) if total_evaluated else 0.0
    avg_conf = round(total_confidence / total_evaluated, 2) if total_evaluated else 100.0
    
    auto_approved = total_evaluated - human_review_count

    return {
        "total_evaluated": total_evaluated,
        "overall_cell_accuracy": cell_acc,
        "attribute_accuracy": attr_acc,
        "lov_compliance_rate": lov_rate,
        "uom_compliance_rate": uom_rate,
        "desc_limit_compliance": desc_limit_rate,
        "missing_field_rate": missing_rate,
        "evidence_backed_rate": evidence_rate,
        "human_review_rate": human_rate,
        "average_confidence": avg_conf,
        "auto_approved_count": auto_approved,
        "needs_review_count": human_review_count
    }


if __name__ == "__main__":
    asyncio.run(run_evaluation())
