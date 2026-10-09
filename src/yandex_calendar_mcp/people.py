"""Поиск людей организации: principal-property-search (RFC 3744) по коллекции principals. Без сети."""

from __future__ import annotations

from xml.etree import ElementTree
from xml.sax.saxutils import escape

from yandex_calendar_mcp.dto import Person
from yandex_calendar_mcp.freebusy import NS


def people_search__build(query: str) -> str:
    """Подстрока ищется и в имени, и в адресе: латинская фамилия находится через логин в e-mail."""
    match = escape(query.strip())
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:principal-property-search xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" test="anyof">'
        f'<D:property-search><D:prop><D:displayname/></D:prop><D:match>{match}</D:match></D:property-search>'
        '<D:property-search><D:prop><C:calendar-user-address-set/></D:prop>'
        f'<D:match>{match}</D:match></D:property-search>'
        '<D:prop><D:displayname/><C:calendar-user-address-set/></D:prop>'
        '</D:principal-property-search>'
    )


def people_search__parse(xml: str) -> list[Person]:
    """Один D:response на человека. Без mailto-адреса запись пропускается: пригласить его всё равно нельзя."""
    root = ElementTree.fromstring(xml)
    people: list[Person] = []
    for response in root.findall('D:response', NS):
        name = response.findtext('.//D:displayname', default='', namespaces=NS).strip()
        hrefs = [
            href.text.strip() for href in response.findall('.//C:calendar-user-address-set/D:href', NS) if href.text
        ]
        emails = [href[len('mailto:') :].lower() for href in hrefs if href.lower().startswith('mailto:')]
        if emails:
            people.append(Person(name=name or None, email=emails[0]))
    return people
