from app.ingest import chunk_text, empty_source_message, extract_html, is_template_fragment


MENU_HTML = """
<html><body>
  <div class="page-header">
    <div>ОЕИРЦ</div>
    <div>«Областной Единый Информационно-Расчетный Центр»</div>
    <div>Акционерное общество</div>
  </div>
  <div>
    <a href="/">Главная</a>
    <a href="/zhku">Расчет ЖКУ</a>
    <a href="/info">Раскрытие информации</a>
    <a href="/lk">Личный кабинет</a>
    <a href="/passenger">Личный кабинет пассажира</a>
    <a href="/contacts">Контакты</a>
  </div>
  <h1>Социальная транспортная карта</h1>
  <p>Льготный проездной билет для школьника стоит 750 рублей и действует один календарный месяц.</p>
  <p>Подробности опубликованы на https://oeirc.ru/?page=tk/stk.php для пассажиров области.</p>
</body></html>
""".encode("utf-8")


def test_html_menu_is_absent_from_chunks():
    chunks = chunk_text(extract_html(MENU_HTML), chunk_size=500)
    blob = "\n".join(chunks)
    assert chunks
    assert "Личный кабинет" not in blob
    assert "Раскрытие информации" not in blob
    assert "750" in blob
    assert "https://oeirc.ru/?page=tk/stk.php" in blob


def test_long_paragraph_keeps_words_and_urls():
    url = "https://oeirc.ru/tk/docs/rules.pdf"
    words = ["проездной"] * 80
    words[20] = url
    paragraph = " ".join(words)
    chunks = chunk_text(paragraph, chunk_size=80)
    assert len(paragraph) > 80
    assert chunks
    for chunk in chunks:
        assert all(token in {"проездной", url} for token in chunk.split())
        assert not chunk.startswith("проезд") or chunk.startswith("проездной")
    assert any(url in chunk.split() for chunk in chunks)
    assert all("https://oeirc.ru/tk/docs/rul " not in f"{chunk} " for chunk in chunks)


def test_overlap_repeats_a_whole_paragraph():
    paragraphs = [
        "Первый абзац про льготный проездной билет для школьников области.",
        "Второй абзац объясняет срок действия с пятнадцатого по пятое число.",
        "Третий абзац указывает стоимость семьсот пятьдесят рублей в месяц.",
    ]
    chunks = chunk_text("\n\n".join(paragraphs), chunk_size=140)
    assert len(chunks) >= 2
    for chunk in chunks:
        for part in chunk.split("\n\n"):
            assert part in paragraphs


def test_table_rows_keep_header():
    table = "\n".join([
        "Категория | Цена | Срок",
        "Школьник | 750 | месяц",
        "Пенсионер | 750 | месяц",
        "Студент | 900 | месяц",
    ])
    chunks = chunk_text(table, chunk_size=60)
    assert len(chunks) >= 2
    assert all(chunk.startswith("Категория | Цена | Срок") for chunk in chunks)
    assert any("Школьник" in chunk for chunk in chunks)
    assert any("Студент" in chunk for chunk in chunks)


SPA_HTML = """
<html><body>
  <h1>Схемы маршрутов</h1>
  <p>Выберите номер маршрута</p>
  <p>{{ num }}</p>
  <p>Начало маршрута {{ station }}</p>
  <p>Загрузка маршрутов...</p>
  <p>{{ route.number }} {{ route.name }}</p>
</body></html>
""".encode("utf-8")


def test_spa_template_chunk_is_not_indexed():
    chunks = [part for part in chunk_text(extract_html(SPA_HTML)) if not is_template_fragment(part)]
    blob = "\n".join(chunks)
    assert "{{" not in blob
    assert "Загрузка маршрутов" not in blob
    assert chunks == []


def test_doc_without_text_is_skipped_with_reason():
    message = empty_source_message("Старый регламент", "doc")
    assert ".doc" in message
    assert "не пишется" in message
