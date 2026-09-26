from app.language.glossary import protect_english, restore, rw_terms_to_english


def test_kinyarwanda_animal_names_are_not_left_to_the_translator() -> None:
    assert rw_terms_to_english("Ni hehe imvubu iri?") == "Ni hehe hippopotamus iri?"
    assert rw_terms_to_english("Imparage zingahe?") == "zebra zingahe?"


def test_english_animals_and_times_round_trip_through_placeholders() -> None:
    text, slots = protect_english("Up to 2 hippopotamus were seen from 24:00 to 28:10, and 10 plains zebras.")
    assert "hippopotamus" not in text and "24:00" not in text and "(T1)" in text and "animals (A1)" in text
    translated = "Inyamaswa zigera kuri 2 (A1) zagaragaye kuva (T1) kugeza (T2), n'inyamaswa 10 (A2)."  # what NLLB returns
    assert restore(translated, slots) == "Inyamaswa zigera kuri 2 (imvubu) zagaragaye kuva 24:00 kugeza 28:10, n'inyamaswa 10 (imparage)."
