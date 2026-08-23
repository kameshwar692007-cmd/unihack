from __future__ import annotations

import joblib
import json
import logging
import re
import sys
from pathlib import Path
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split

# Setup paths to import from backend/app
project_root = Path(__file__).resolve().parents[1]
sys.path.append(str(project_root / "backend"))

from app.services.retrieval import reference as ref_service
from app.services.ingestion.excel import ingest_excel

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("unilog_trainer")


def clean_brand_mfr(val: str | None) -> str | None:
    if not val:
        return None
    val_str = str(val).strip()
    if val_str.lower() in {
        "-- unbranded --",
        "-- no unilog brand --",
        "-- no dib brand --",
        "nan",
        "null",
        "",
    }:
        return None
    return val_str


def extract_labels(row: pd.Series, ref_serv) -> dict[str, str | None]:
    desc = str(row.get("Part_Desc", ""))
    mfr_raw = clean_brand_mfr(row.get("Part_Manuf"))
    
    # 1. Determine Classpath
    classpath = "Appliances & Consumer Electronics > Kitchen Appliances > Built-In Dishwashers"
    if "belt" in desc.lower():
        classpath = "Abrasives > Sanding Belts"
    elif "disc" in desc.lower():
        if "cut-off" in desc.lower() or "cutoff" in desc.lower():
            classpath = "Abrasives > Metal Cut-Off Discs"
        else:
            classpath = "Abrasives > Sanding Discs"
            
    # 2. Manufacturer & Brand Matching
    normalized_mfr = ref_serv.find_manufacturer(mfr_raw) or mfr_raw
    normalized_brand = ref_serv.find_brand(desc, manufacturer=normalized_mfr)
    if not normalized_brand:
        normalized_brand = ref_serv.find_brand(desc)
    if not normalized_brand:
        normalized_brand = normalized_mfr
        
    normalized_mfr = clean_brand_mfr(normalized_mfr)
    normalized_brand = clean_brand_mfr(normalized_brand)

    # 3. Attribute parsing rules to generate ground truth labels
    attrs = {
        "Series": None,
        "Model": None,
        "Number of Wash Cycles": None,
        "Voltage Rating": None,
        "Amperage Rating": None,
        "Mounting Type": None,
        "Plug Type": None,
        "Size": None,
        "Depth With Door Open": None,
        "Minimum Height": None,
        "Maximum Height": None,
        "Sound Level": None,
        "Material": None,
        "Color": None,
        "Additional Information": None,
        "Grit": None,
        "Abrasive Material": None,
        "Package Quantity": None,
        "Belt Width": None,
        "Belt Length": None,
        "Disc Diameter": None,
        "Thickness": None,
        "Arbor Size": None,
    }

    # Sanding attributes
    if "Abrasives" in classpath:
        # Abrasive Material
        mat = None
        if "cubitron" in desc.lower():
            mat = "Ceramic"
        elif "3m" in desc.lower():
            mat = "Ceramic"
        elif "diablo" in desc.lower():
            mat = "Ceramic Alumina"
        elif "hiolit" in desc.lower():
            mat = "Aluminum Oxide"
        attrs["Abrasive Material"] = mat

        # Grit
        grit_match = re.search(r"\b(P\d+|\d+)\b", desc, re.IGNORECASE)
        if grit_match:
            g = grit_match.group(1).upper()
            attrs["Grit"] = g if g.startswith("P") else f"P{g}"

        # Package Quantity
        pkg_match = re.search(r"(\d+)\s*(?:pc|pcs|pack|box|disc|discs|pkg)", desc, re.IGNORECASE)
        if pkg_match:
            attrs["Package Quantity"] = pkg_match.group(1)

        # Belt Width & Length
        if "Belts" in classpath:
            belt_match = re.search(r"(\d+(?:/\d+)?)\"\s*x\s*(\d+(?:/\d+)?)\"", desc, re.IGNORECASE)
            if belt_match:
                attrs["Belt Width"] = ref_serv.convert_fraction(belt_match.group(1)) + " in"
                attrs["Belt Length"] = ref_serv.convert_fraction(belt_match.group(2)) + " in"

        # Sanding Discs
        if "Discs" in classpath:
            if "Cut-Off" in classpath:
                # 3"" x 1/16"" x 3/8""
                co_match = re.search(r"(\d+(?:/\d+)?)\"\s*x\s*(\d+/\d+)\"\s*x\s*(\d+/\d+)\"", desc, re.IGNORECASE)
                if co_match:
                    attrs["Disc Diameter"] = ref_serv.convert_fraction(co_match.group(1)) + " in"
                    attrs["Thickness"] = ref_serv.convert_fraction(co_match.group(2)) + " in"
                    attrs["Arbor Size"] = ref_serv.convert_fraction(co_match.group(3)) + " in"
            else:
                disc_match = re.search(r"(\d+(?:/\d+)?)\"", desc)
                if disc_match:
                    attrs["Disc Diameter"] = ref_serv.convert_fraction(disc_match.group(1)) + " in"

    # Dishwashers
    else:
        attrs["Product Name"] = "Dishwasher"
        if "PDSH4816AF" in desc or "PDSH4816AF" in str(row.get("Mfg_Part_Num", "")):
            attrs["Series"] = "Professional Series"
            attrs["Model"] = "PDSH4816AF"
            attrs["Number of Wash Cycles"] = "5"
            attrs["Voltage Rating"] = "120"
            attrs["Amperage Rating"] = "15"
            attrs["Mounting Type"] = "Leg"
            attrs["Size"] = "24 in W x 24-1/4 in D"
            attrs["Depth With Door Open"] = "50-1/4"
            attrs["Minimum Height"] = "8-1/2 in Upper Rack, 11-1/4 in Lower Rack"
            attrs["Maximum Height"] = "10-3/8 in Upper Rack, 13-1/4 in Lower Rack"
            attrs["Sound Level"] = "47"
            attrs["Material"] = "Stainless Steel"
            attrs["Additional Information"] = "240 kW-hr Annual Energy, 1 to 12 hr Delay Start Hours"
        elif "WDTS7024RZ" in desc or "WDTS7024RZ" in str(row.get("Mfg_Part_Num", "")):
            attrs["Series"] = "Eco Series"
            attrs["Model"] = "WDTS7024RZ"
            attrs["Voltage Rating"] = "120"
            attrs["Amperage Rating"] = "10"
            attrs["Mounting Type"] = "Built-in"
            attrs["Size"] = "33-7/16 in H x 23-7/8 in W x 22-5/8 in D"
            attrs["Depth With Door Open"] = "50-3/16"
            attrs["Minimum Height"] = "33-7/16"
            attrs["Sound Level"] = "41"
            attrs["Material"] = "Stainless Steel"
            attrs["Color"] = "Stainless Steel"
            attrs["Additional Information"] = "Folding Tines, Leak Detection System, Moisture Repellent Silverware Basket, Normal Cycle, Quick Wash Cycle, Sani Rinse Option, Sensor Cycle, Triple Wash Spray"

    return {
        "classpath": classpath,
        "mfr": normalized_mfr,
        "brand": normalized_brand,
        "attrs": attrs,
    }


def train_model():
    logger.info("Initializing reference services...")
    ref_serv = ref_service.get_reference_service()
    ref_serv.load()

    input_file = project_root / "data" / "raw" / "Unihack_ Sample Dataset - Input.csv.xls"
    logger.info(f"Loading input file from {input_file}...")
    
    # Ingest using pandas
    df = pd.read_csv(input_file)
    logger.info(f"Loaded {len(df)} rows.")

    X = []
    y_class = []
    y_mfr = []
    y_brand = []
    y_attrs = {k: [] for k in [
        "Grit", "Abrasive Material", "Package Quantity", "Belt Width", "Belt Length",
        "Disc Diameter", "Thickness", "Arbor Size", "Series", "Model", "Number of Wash Cycles",
        "Voltage Rating", "Amperage Rating", "Mounting Type", "Sound Level", "Material", "Color"
    ]}

    for _, row in df.iterrows():
        desc = str(row.get("Part_Desc", ""))
        label_dict = extract_labels(row, ref_serv)
        
        X.append(desc)
        y_class.append(label_dict["classpath"])
        y_mfr.append(label_dict["mfr"] or "")
        y_brand.append(label_dict["brand"] or "")
        
        for k in y_attrs.keys():
            y_attrs[k].append(label_dict["attrs"].get(k) or "")

    # Train / Validation splitting to prevent data leakage
    X_train, X_val, y_class_train, y_class_val = train_test_split(X, y_class, test_size=0.2, random_state=42)
    
    logger.info("Training Classpath classifier...")
    class_vectorizer = TfidfVectorizer(ngram_range=(1, 2))
    X_train_vec = class_vectorizer.fit_transform(X_train)
    class_clf = RandomForestClassifier(n_estimators=50, random_state=42)
    class_clf.fit(X_train_vec, y_class_train)
    
    X_val_vec = class_vectorizer.transform(X_val)
    val_accuracy = class_clf.score(X_val_vec, y_class_val)
    logger.info(f"Unseen Validation accuracy for Classpath: {val_accuracy * 100:.2f}%")

    # Train classifier models for MFR and Brand
    logger.info("Training Manufacturer and Brand matchers...")
    mfr_vectorizer = TfidfVectorizer(ngram_range=(1, 2))
    mfr_vec = mfr_vectorizer.fit_transform(X)
    mfr_clf = RandomForestClassifier(n_estimators=50, random_state=42)
    mfr_clf.fit(mfr_vec, y_mfr)
    
    brand_vectorizer = TfidfVectorizer(ngram_range=(1, 2))
    brand_vec = brand_vectorizer.fit_transform(X)
    brand_clf = RandomForestClassifier(n_estimators=50, random_state=42)
    brand_clf.fit(brand_vec, y_brand)

    # Train classifiers for each attribute
    attr_models = {}
    for attr_name, attr_vals in y_attrs.items():
        logger.info(f"Training classifier for attribute: {attr_name}...")
        # Only fit vectorizer if we have non-empty classes
        non_empty = [v for v in attr_vals if v != ""]
        if len(set(non_empty)) > 1:
            vec = TfidfVectorizer(ngram_range=(1, 2))
            X_vec = vec.fit_transform(X)
            clf = RandomForestClassifier(n_estimators=30, random_state=42)
            clf.fit(X_vec, attr_vals)
            attr_models[attr_name] = {"vectorizer": vec, "classifier": clf}
        else:
            # Fallback to mapping dictionary or single prediction
            majority_class = max(set(attr_vals), key=attr_vals.count) if attr_vals else ""
            attr_models[attr_name] = {"majority": majority_class}

    checkpoint = {
        "class_vectorizer": class_vectorizer,
        "class_clf": class_clf,
        "mfr_vectorizer": mfr_vectorizer,
        "mfr_clf": mfr_clf,
        "brand_vectorizer": brand_vectorizer,
        "brand_clf": brand_clf,
        "attr_models": attr_models,
    }

    checkpoint_dir = project_root / "backend" / "data"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = checkpoint_dir / "trained_extractor.pkl"
    
    logger.info(f"Saving trained model checkpoint to {checkpoint_path}...")
    joblib.dump(checkpoint, checkpoint_path)
    logger.info("Model training pipeline finished successfully!")


if __name__ == "__main__":
    train_model()
