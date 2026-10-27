import importlib
import time

import pytest

from webui.mass_search import ensure_mass_index, mass_window, paths_in_mass_window


# Independent monoisotopic mass arithmetic for two distinct, near-isobaric
# formulas. Their midpoint puts both inside 10 ppm, but outside 5 ppm.
FORMULA_A = "C40H40O10"
FORMULA_B = "C39H40N2O9"
MASS_A = 40 * 12 + 40 * 1.007825032 + 10 * 15.994914684
MASS_B = 39 * 12 + 40 * 1.007825032 + 2 * 14.003074 + 9 * 15.994914684
CENTER = (MASS_A + MASS_B) / 2
PROTON = 1.007276452


@pytest.fixture
def atlas(monkeypatch, tmp_path):
    monkeypatch.setenv("FLASK_SECRET_KEY", "mass-search-test")
    module = importlib.import_module("webui.app")
    public, nist = tmp_path / "public", tmp_path / "nist"
    public.mkdir()
    nist.mkdir()
    monkeypatch.setenv("ICEBERG_MASS_INDEX_DIR", str(tmp_path / "indexes"))
    monkeypatch.setattr(module, "ADDUCT_TO_DIR", {"[M+H]+": public, "[M-H]-": public})
    monkeypatch.setattr(module, "NIST_MGF_DIR", nist)
    monkeypatch.setattr(module, "NIST_ADDUCT_TO_DIR", {"[M+H]+": nist, "[M-H]-": nist})
    monkeypatch.setattr(module, "JOB_STORE_DIR", tmp_path / "jobs")
    monkeypatch.setattr(module, "_ANALYTICS_DB_PATH", None)
    monkeypatch.setattr(module, "_load_users", lambda: {
        "plain@example.com": {"role": "user"},
        "nist@example.com": {"role": "authorized_user"},
    })
    monkeypatch.setitem(module.app.config, "TESTING", True)
    monkeypatch.setitem(module.app.config, "SECRET_KEY", "mass-search-test")
    return module, public, nist


def write_mgf(module, root, formula, key, peaks="100 100\n150 100", ces=(41,), adduct="[M+H]+"):
    path = root / module.common.get_formula_subdir(formula) / f"{formula}.mgf"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        for ce in ces:
            handle.write(
                f"BEGIN IONS\nFORMULA={formula}\nINCHIKEY={key}\n"
                f"SMILES=CCO\nADDUCT={adduct}\nCOLLISION_ENERGY={ce}\n"
                f"{peaks}\nEND IONS\n"
            )
    return path


def run_job(module, client, **overrides):
    data = {
        "search_mode": "mass", "query_mass": str(CENTER + PROTON),
        "mass_type": "mz", "mass_ppm": "10", "adduct": "[M+H]+",
        "spectrum_text": "100 100\n150 100", "spectrum_nce": "30",
        "spectrum_mode": "single", "spectrum_stepped_nces": "",
    }
    data.update(overrides)
    response = client.post("/api/retrieve_start", data=data)
    assert response.status_code == 200
    job_id = response.get_json()["job_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = module._job_get(job_id)
        if job.status in ("done", "error"):
            return job_id, job
        time.sleep(0.01)
    pytest.fail("Retrieval job did not finish")


@pytest.mark.parametrize("adduct,shift", [("[M+H]+", PROTON), ("[M-H]-", -PROTON)])
@pytest.mark.parametrize("ppm", [3, 5, 10, 20])
def test_mass_units_and_ppm_denominator(adduct, shift, ppm):
    lower, upper, precursor = mass_window(CENTER + shift, ppm, "mz", adduct)
    assert (lower + upper) / 2 == pytest.approx(CENTER)
    assert upper - lower == pytest.approx(2 * (CENTER + shift) * ppm / 1e6)
    assert precursor == pytest.approx(CENTER + shift)
    lower, upper, precursor = mass_window(CENTER, ppm, "neutral", adduct)
    assert upper - lower == pytest.approx(2 * CENTER * ppm / 1e6)
    assert precursor == pytest.approx(CENTER + shift)


@pytest.mark.parametrize("mass,ppm,kind,adduct", [
    ("", 10, "mz", "[M+H]+"), ("nan", 10, "mz", "[M+H]+"),
    ("inf", 10, "mz", "[M+H]+"), (-1, 10, "mz", "[M+H]+"),
    (0, 10, "mz", "[M+H]+"), (0.5, 10, "mz", "[M+H]+"),
    (100, 15, "mz", "[M+H]+"), (100, "nan", "mz", "[M+H]+"),
    (100, 10, "average", "[M+H]+"), (100, 10, "mz", "[M+Na]+"),
])
def test_invalid_mass_query(mass, ppm, kind, adduct):
    with pytest.raises(ValueError):
        mass_window(mass, ppm, kind, adduct)


def test_index_exhaustive_window_boundaries_and_refresh(atlas):
    module, public, _ = atlas
    a = write_mgf(module, public, FORMULA_A, "A")
    b = write_mgf(module, public, FORMULA_B, "B")
    write_mgf(module, public, "C40H42O10", "outside")
    lower, upper, _ = mass_window(CENTER, 10, "neutral")
    assert {f for f, _ in paths_in_mass_window(public, lower, upper)} == {FORMULA_A, FORMULA_B}
    lower, upper, _ = mass_window(CENTER, 5, "neutral")
    assert paths_in_mass_window(public, lower, upper) == []
    exact = module.common.formula_mass(FORMULA_A)
    assert paths_in_mass_window(public, exact, exact) == [(FORMULA_A, a.resolve())]
    # Index snapshots contain filenames, not copied spectra, and are reusable.
    index = ensure_mass_index(public)
    stamp = index.stat().st_mtime_ns
    assert ensure_mass_index(public).stat().st_mtime_ns == stamp
    b.unlink()
    with pytest.raises(FileNotFoundError, match="rebuild"):
        paths_in_mass_window(public, MASS_B - 0.001, MASS_B + 0.001)
    ensure_mass_index(public, rebuild=True)
    assert paths_in_mass_window(public, MASS_B - 0.001, MASS_B + 0.001) == []


def test_mass_search_global_ranking_and_mirror_payload(atlas):
    module, public, _ = atlas
    write_mgf(module, public, FORMULA_A, "A", "200 100\n250 50")
    write_mgf(module, public, FORMULA_B, "B")
    write_mgf(module, public, "C40H42O10", "outside")
    client = module.app.test_client()
    # A stale formula must not restrict a mass search.
    job_id, job = run_job(module, client, formula="C2H6O")
    assert job.status == "done", job.error
    ctx = job.result_context
    assert [h["inchikey"] for h in ctx["results"]] == ["B", "A"]
    assert ctx["results"][0]["similarity"] == pytest.approx(1)
    assert ctx["results"][1]["similarity"] == pytest.approx(0, abs=1e-7)
    summary = ctx["retrieval_data"]["search_summary"]
    assert set(summary["formulas"]) == {FORMULA_A, FORMULA_B}
    assert summary["candidate_count"] == summary["ranked_count"] == 2
    assert ctx["formula"] == ""
    assert ctx["retrieval_data"]["exp_spectra"][0]["collision_energy_ev"] == 41
    page = client.get(f"/result/{job_id}")
    assert page.status_code == 200
    assert f'data-formula="{FORMULA_B}"'.encode() in page.data
    assert b'2 candidate structures; 2 ranked' in page.data
    assert b'id="formula" name="formula" disabled' in page.data


def test_mass_search_ranks_full_pool_before_top_50(atlas):
    module, public, _ = atlas
    for index in range(55):
        write_mgf(module, public, FORMULA_A, f"A{index}", "200 100\n250 50")
    write_mgf(module, public, FORMULA_B, "best")
    _, job = run_job(module, module.app.test_client())
    assert job.status == "done", job.error
    assert len(job.result_context["results"]) == 50
    assert job.result_context["results"][0]["inchikey"] == "best"
    summary = job.result_context["retrieval_data"]["search_summary"]
    assert summary["candidate_count"] == summary["ranked_count"] == 56


@pytest.mark.parametrize("role,expected", [(None, {"A"}), ("plain", {"A"}), ("nist", {"A", "N"})])
def test_mass_search_gates_nist_and_deduplicates(atlas, role, expected):
    module, public, nist = atlas
    write_mgf(module, public, FORMULA_A, "A")
    write_mgf(module, nist, FORMULA_A, "A", "200 100")  # Public copy wins.
    write_mgf(module, nist, FORMULA_B, "N")
    client = module.app.test_client()
    if role:
        with client.session_transaction() as session:
            session["_user_id"] = f"{role}@example.com"
            session["_fresh"] = True
    _, job = run_job(module, client)
    assert job.status == "done", job.error
    assert {h["inchikey"] for h in job.result_context["results"]} == expected
    public_hit = next(h for h in job.result_context["results"] if h["inchikey"] == "A")
    assert public_hit["similarity"] == pytest.approx(1)


def test_formula_mode_remains_default(atlas):
    module, public, _ = atlas
    write_mgf(module, public, FORMULA_A, "A")
    write_mgf(module, public, FORMULA_B, "B")
    client = module.app.test_client()
    _, job = run_job(module, client, search_mode="formula", formula=FORMULA_A, query_mass="garbage")
    assert job.status == "done", job.error
    assert [h["inchikey"] for h in job.result_context["results"]] == ["A"]
    data = {"formula": FORMULA_A, "spectrum_text": "100 100", "spectrum_nce": "30"}
    response = client.post("/api/retrieve_start", data=data)
    job_id = response.get_json()["job_id"]
    deadline = time.monotonic() + 10
    while module._job_get(job_id).status not in ("done", "error") and time.monotonic() < deadline:
        time.sleep(0.01)
    job = module._job_get(job_id)
    assert job.status == "done", job.error
    assert job.result_context["search_mode"] == "formula"
    assert b'<option value="formula" selected>' in client.get("/").data


def test_no_formula_match_shows_empty_result(atlas):
    module, public, _ = atlas
    write_mgf(module, public, FORMULA_A, "A")
    client = module.app.test_client()
    job_id, job = run_job(module, client, mass_ppm="5")
    assert job.status == "done", job.error
    assert job.result_context["results"] == []
    assert b"No atlas formulas match this mass window." in client.get(f"/result/{job_id}").data


@pytest.mark.parametrize("mass_type,adduct,shift", [
    ("mz", "[M-H]-", -PROTON), ("neutral", "[M+H]+", PROTON),
])
def test_alternate_mass_units_and_polarity(atlas, mass_type, adduct, shift):
    module, public, _ = atlas
    write_mgf(module, public, FORMULA_A, "A", adduct=adduct)
    write_mgf(module, public, FORMULA_B, "B", adduct=adduct)
    _, job = run_job(module, module.app.test_client(), mass_type=mass_type, adduct=adduct,
                     query_mass=str(CENTER + shift if mass_type == "mz" else CENTER))
    assert job.status == "done", job.error
    assert len(job.result_context["results"]) == 2


def test_stepped_mode_loads_all_requested_energies(atlas):
    module, public, _ = atlas
    write_mgf(module, public, FORMULA_A, "A", ces=(27, 41, 68))
    _, job = run_job(module, module.app.test_client(), spectrum_mode="stepped", spectrum_stepped_nces="20,30,50")
    assert job.status == "done", job.error
    hit = job.result_context["results"][0]
    assert hit["collision_energy"] == "27, 41, 68"
    assert len(hit["pred_spectra"]) == 3


def test_skip_empty_query_blocks_and_reject_all_empty(atlas):
    module, public, _ = atlas
    write_mgf(module, public, FORMULA_A, "A", ces=(41, 68))
    client = module.app.test_client()
    _, job = run_job(module, client, spectrum_text=["100 100\n150 100", "900 100"],
                     spectrum_nce=["30", "50"], spectrum_mode=["single", "single"],
                     spectrum_stepped_nces=["", ""])
    assert job.status == "done", job.error
    assert job.result_context["retrieval_data"]["search_summary"]["skipped_empty_blocks"] == 1
    assert job.result_context["results"][0]["collision_energy"] == "41"
    _, job = run_job(module, client, spectrum_text="900 100")
    assert job.status == "error"
    assert "empty after preprocessing" in job.error
