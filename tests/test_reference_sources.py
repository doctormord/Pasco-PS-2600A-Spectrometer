import numpy as np
import pytest

import reference_sources as rs

PAIRS = [(300.0, 1.5), (350.0, 2.25), (400.0, 4.0), (450.0, 8.5)]


def _text(sep, dec, eol="\n", index=False, header=False):
    lines = []
    if header:
        lines.append("Wavelength" + sep + "Value")
    for i, (w, v) in enumerate(PAIRS):
        f = lambda x: repr(x).replace(".", dec)
        cols = ([str(i)] if index else []) + [f(w), f(v)]
        lines.append(sep.join(cols))
    return eol.join(lines) + eol


def _check(wl, val):
    np.testing.assert_allclose(wl, [p[0] for p in PAIRS])
    np.testing.assert_allclose(val, [p[1] for p in PAIRS])


def test_parse_lmp_file(lmp_path):
    wl, val = rs.parse_lmp(lmp_path)
    assert len(wl) == 441
    assert wl[0] == 300 and wl[-1] == 2500
    i = int(np.argmax(val))
    assert wl[i] == 1250
    assert val[i] == pytest.approx(401.251677)
    wl2, val2 = rs.parse_reference_file(lmp_path)
    np.testing.assert_array_equal(wl, wl2)


@pytest.mark.parametrize("sep,dec,eol,index,header", [
    ("\t", ",", "\r\n", False, False),
    (" ", ".", "\n", False, False),
    ("  ", ",", "\r\n", False, False),
    ("\t", ".", "\n", True, False),
    (" ", ",", "\r\n", True, True),
    ("\t", ".", "\n", False, True),
])
def test_parse_lmp_variants(sep, dec, eol, index, header):
    _check(*rs.parse_lmp(_text(sep, dec, eol, index, header)))


def test_parse_lmp_unsorted_duplicates_short():
    wl, _ = rs.parse_lmp("400 1\n300 2\n350 3\n")
    assert list(wl) == [300, 350, 400]
    with pytest.raises(ValueError):
        rs.parse_lmp("300 1\n300 2\n")
    with pytest.raises(ValueError):
        rs.parse_lmp("header only\n300 1\n")


@pytest.mark.parametrize("sep,dec,header", [
    (";", ",", False), (";", ",", True), (",", ".", False), (",", ".", True),
    ("\t", ",", True), ("\t", ".", False),
])
def test_parse_csv_variants(sep, dec, header):
    _check(*rs.parse_csv(_text(sep, dec, "\r\n", False, header)))


def test_parse_reference_file_csv_and_missing(tmp_path):
    p = tmp_path / "lamp.csv"
    p.write_text(_text(";", ","), encoding="utf-8")
    _check(*rs.parse_reference_file(str(p)))
    with pytest.raises(ValueError):
        rs.parse_reference_file(str(tmp_path / "nope.csv"))


def test_interpolate_exact_monotone_nan():
    wl = np.array([300.0, 350, 400, 450, 500])
    val = np.array([1.0, 1.1, 5.0, 5.1, 5.2])  # step-like, monotone
    out = rs.interpolate_to_axis(wl, val, wl)
    np.testing.assert_allclose(out, val)
    fine = np.arange(300, 500.5, 1.0)
    o = rs.interpolate_to_axis(wl, val, fine)
    assert np.all(np.diff(o) >= -1e-12)
    assert o.min() >= 1.0 - 1e-12 and o.max() <= 5.2 + 1e-12
    o = rs.interpolate_to_axis(wl, val, [250, 300, 400, 500, 600], 320, 480)
    assert np.isnan(o[0]) and np.isnan(o[1]) and np.isnan(o[3]) and np.isnan(o[4])
    assert o[2] == pytest.approx(5.0)
    o = rs.interpolate_to_axis(wl, val, [250, 300, 500, 600])
    assert np.isnan(o[0]) and np.isnan(o[3]) and not np.isnan(o[1]) and not np.isnan(o[2])


def test_convert_irradiance():
    assert rs.convert_irradiance([1.0], rs.UNIT_UW_CM2_NM, rs.UNIT_W_M2_NM)[0] == pytest.approx(0.01)
    assert rs.convert_irradiance([1.0], rs.UNIT_UW_CM2_NM, "mW/m2/nm")[0] == pytest.approx(10.0)
    v = np.array([1.0, 2.5, 7.0])
    for u in rs.KNOWN_UNITS[:3]:
        back = rs.convert_irradiance(rs.convert_irradiance(v, rs.UNIT_UW_CM2_NM, u), u, rs.UNIT_UW_CM2_NM)
        np.testing.assert_allclose(back, v)
    np.testing.assert_allclose(rs.convert_irradiance(v, "relative", "relative"), v)
    with pytest.raises(ValueError):
        rs.convert_irradiance(v, "relative", rs.UNIT_UW_CM2_NM)
    with pytest.raises(ValueError):
        rs.convert_irradiance(v, rs.UNIT_W_M2_NM, "relative")
    with pytest.raises(ValueError):
        rs.convert_irradiance(v, "foo", rs.UNIT_W_M2_NM)


def test_library_roundtrip(lmp_path, tmp_path):
    path = str(tmp_path / "lib.json")
    lib = rs.ReferenceLibrary(path)
    src = lib.import_file(lmp_path, name="HAL-CAL", serial="1808001",
                          valid_min_nm=380, input_optic="CC")
    assert src.source_file == "1808001-NIR-CCVISNIR.lmp"
    assert src.imported[-6] in "+-"  # timezone offset present
    lib2 = rs.ReferenceLibrary(path)
    got = lib2.get(src.id)
    assert got is not None and not got.builtin
    np.testing.assert_array_equal(got.wl, src.wl)
    np.testing.assert_array_equal(got.values, src.values)
    assert got.valid_min_nm == 380 and got.valid_max_nm == 2500
    assert got.input_optic == "CC"
    out = got.on_axis([350.0, 379.0, 380.0, 1250.0, 2600.0])
    assert np.isnan(out[0]) and np.isnan(out[1]) and np.isnan(out[4])
    assert out[3] == pytest.approx(401.251677)
    w = got.on_axis([1250.0], unit=rs.UNIT_W_M2_NM)
    assert w[0] == pytest.approx(4.01251677)
    assert got.data_range_nm == (300.0, 2500.0)
    ids = [s.id for s in lib2.list()]
    assert ids[0] == rs.DEMO_SOURCE_ID and src.id in ids
    assert lib2.remove(src.id) is True
    assert lib2.remove(src.id) is False
    assert rs.ReferenceLibrary(path).get(src.id) is None


def test_library_default_path_from_config(isolated_config):
    lib = rs.ReferenceLibrary()
    assert lib.path == isolated_config.get("reference_sources_file")


def test_builtin():
    lib = rs.ReferenceLibrary()
    b = lib.get(rs.DEMO_SOURCE_ID)
    assert b is not None and b.builtin and b.source_file == "builtin"
    assert b.serial == "DEMO-LAMP-1" and b.input_optic == "CC"
    assert b.data_range_nm == (300.0, 1100.0) and len(b.wl) == 801
    assert (b.valid_min_nm, b.valid_max_nm) == (350.0, 1000.0)
    assert lib.remove(rs.DEMO_SOURCE_ID) is False
    lib.add(rs.ReferenceSource.from_dict({**b.to_dict(), "id": "x", "name": "X"}))
    import json
    with open(lib.path) as f:
        ids = [s["id"] for s in json.load(f)["sources"]]
    assert ids == ["x"]
    with pytest.raises(ValueError):
        lib.add(b)


def test_import_validation(lmp_path):
    lib = rs.ReferenceLibrary()
    kw = dict(name="L", serial="1")
    with pytest.raises(ValueError):
        lib.import_file(lmp_path, valid_min_nm=900, valid_max_nm=800, **kw)
    with pytest.raises(ValueError):
        lib.import_file(lmp_path, valid_min_nm=200, **kw)
    with pytest.raises(ValueError):
        lib.import_file(lmp_path, valid_max_nm=3000, **kw)
    with pytest.raises(ValueError):
        lib.import_file(lmp_path, unit="lux", **kw)
    with pytest.raises(ValueError):
        lib.import_file(lmp_path, input_optic="fibre", **kw)
    assert [s.id for s in lib.list()] == [rs.DEMO_SOURCE_ID]


def test_dict_roundtrip():
    b = rs.builtin_sources()[0]
    d = b.to_dict()
    import json
    b2 = rs.ReferenceSource.from_dict(json.loads(json.dumps(d)))
    np.testing.assert_array_equal(b.values, b2.values)
    with pytest.raises(ValueError):
        rs.ReferenceSource.from_dict({"id": "x"})
