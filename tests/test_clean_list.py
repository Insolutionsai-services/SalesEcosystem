from salescore.services import clean_list

CSV = """Business Name,Owner,Mobile,Monthly Sales,Staff,City
Sri Paints,Ravi,+91 98480 11223,500000,12,Hyderabad
Lakshmi Hardware,Sita,0 94400 22334,1500000,30,Vijayawada
Dup Traders,Anil,9848011223,200000,3,Guntur
Fake One,X,9999999999,900000,50,Hyd
Fake Two,X,1234567890,900000,50,Hyd
Landline Co,Y,040 2345 6789,900000,50,Hyd
Short Co,Z,98765,100,1,Hyd
Two Numbers,Q,"040 1111 2222, 98123 45678",800000,20,Hyd
Empty Co,W,,100,1,Hyd
"""


def test_clean_list_keeps_real_mobiles_ranks_and_explains_rejections():
    kept, rejected, ranks = clean_list.clean(CSV)
    assert ranks == ["Monthly Sales", "Staff"]                                        # size columns found by name
    assert [r["phone"] for r in kept] == ["9440022334", "9812345678", "9848011223"]   # biggest first; duplicate and fakes gone
    assert [r["lead_rank"] for r in kept] == [1, 2, 3] and kept[0]["business"] == "Lakshmi Hardware" and kept[0]["consent"] == ""
    why = {r["Business Name"]: r["rejected_because"] for r in rejected}
    assert why["Dup Traders"] == "duplicate number" and why["Fake One"] == "fake-looking number" and "not a mobile" in why["Fake Two"]
    assert "landline" in why["Landline Co"] and why["Short Co"] == "wrong length" and why["Empty Co"] == "no phone number"
    assert clean_list.mobile("+91-98480-11223") == ("9848011223", "")


def test_a_second_phone_column_cannot_replace_the_checked_number():
    from salescore.services import clean_list
    out, _, _ = clean_list.clean("mobile,phone,name\n9876501234,080-22223333,Ravi\n")
    assert out[0]["phone"].endswith("9876501234")
