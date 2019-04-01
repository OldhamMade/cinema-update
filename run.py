import ConfigParser

from datetime import (
    datetime,
    date,
    timedelta,
)
from collections import (
    defaultdict,
    namedtuple,
)
from string import Template
from urllib import (
    unquote,
)

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

import grequests

from requests import post
from lxml import etree


## Constants
ONE_WEEK = 7 # days


## Config
CONFIG = ConfigParser.SafeConfigParser()
CONFIG.read("settings.ini")


class between(namedtuple("between", "at until")):
    """Time range"""


class Availability(object):
    def get(self, name):
        at, until = CONFIG.get('availability', name.lower()).split(",")
        return between(
            datetime.strptime(at.strip(), '%H:%M').time(),
            datetime.strptime(until.strip(), '%H:%M').time(),
        )


class Templates(object):
    base = Template(open("templates/base.html", "r").read())
    movie = Template(open("templates/movie.html", "r").read())
    entry = Template(open("templates/entry.html", "r").read())


class URLs(object):
    listings = "https://en.pathe.nl/update-schedule/1,2,9,10/{date}"
    tickets = "https://en.pathe.nl{url}"
    imdb = "https://www.imdb.com{path}"
    imdb_query = "https://www.imdb.com/find?exact=true&s=tt&q={title}"


def xpath(value, expr):
    if not hasattr(value, "tree") and isinstance(value, basestring):
        text = StringIO(value.encode('ascii', 'xmlcharrefreplace'))
        parser = etree.HTMLParser()
        value = etree.parse(text, parser)

    try:
        return value.xpath(expr)
    except AttributeError:
        raise Exception("Cannot perform XPath on Value: %s" % value)


def gather_listings(dates):
    reqs = (grequests.get(URLs.listings.format(date=d)) for d in dates)
    responses = grequests.map(reqs)
    return [(resp.url, resp.text) for resp in responses]


def extract_data(listings):
    all_showings = {}

    for url, listing in listings:
        day = datetime.strptime(url.split("/")[-1], "%d-%m-%Y").date()
        showings = extract_showings(listing)
        all_showings[day] = showings

    return all_showings


def extract_showings(data):
    return [
        {
            "name": extract_showing_name(showing),
            "times": extract_showing_times(showing),
            "image": extract_showing_image(showing),
        }
        for showing
        in xpath(data, "//div[@class=\"schedule__section\"]")
    ]


def extract_showing_name(data):
    return xpath(data, ".//h4/a/text()")[0]


def extract_showing_times(data):
    times = []
    cinema = "UNDEFINED LOCATION"
    for schedules in xpath(data, ".//div[@class=\"schedule__wrapper\"]"):
        for el in xpath(schedules, "./*"):
            if el.tag == "p":
                cinema = el.text
                continue

            for schedule in xpath(el, ".//a"):
                start = xpath(schedule, ".//h5/span")[0].text
                end = xpath(schedule, ".//h5/span")[1].text

                times.append({
                    "cinema": cinema,
                    "book": schedule.attrib["data-href"],
                    "start": datetime.strptime(start, "%H:%M").time(),
                    "end": datetime.strptime(end, "%H:%M").time(),
                })

    return times


def extract_showing_image(data):
    return xpath(data, ".//img/@src")[0]


def map_name(url):
    urlbase = URLs.imdb_query.format(title="")
    return unquote(url.replace(urlbase, "")).decode('utf8')


def extract_language(data):
    try:
        return xpath(
            data,
            '//h4[text()="Language:"]/following-sibling::a'
        )[0].text
    except Exception:
        return "English"


def add_imdb_details(data):
    reqs = (grequests.get(URLs.imdb_query.format(title=t)) for t in data)
    responses = grequests.map(reqs)

    lookups = dict(
        (extract_imdb_url(resp.text), map_name(resp.url))
        for resp
        in responses
    )

    reqs = (grequests.get(k) for k in lookups.keys() if k)
    responses = grequests.map(reqs)

    for resp in responses:
        lookup = lookups[resp.url]
        language = extract_language(resp.text)

        for day in data[lookup]:
            data[lookup][day]["language"] = language

    return data


def extract_imdb_url(data):
    try:
        path = xpath(data, '//table[@class="findList"]//a')[0].attrib["href"]
        return URLs.imdb.format(path=path)
    except Exception:
        return None


def filter_by_language(data):
    approved_languages = [
        lang.strip().lower()
        for lang
        in CONFIG.get("languages", "approved").split(",")
    ]

    cleaned = {}

    for movie, dates in data.iteritems():
        for day, details in dates.iteritems():
            if details.get("language", "").lower() in approved_languages:
                cleaned[movie] = dates
                break
    return cleaned


def reformat_data(data):
    """Reformat data to make 'movie-title' top-level."""
    formatted = defaultdict(lambda: {})
    availability = Availability()

    for day, items in data.iteritems():
        available = availability.get(day.strftime("%a"))

        for movie in items:
            relevant = []

            for showing in movie["times"]:
                if showing["start"] < available.at:
                    continue
                if showing["start"] > available.until:
                    continue
                relevant.append(showing)

            if relevant:
                formatted[movie["name"]][day] = {
                    "image": movie["image"],
                    "times": relevant
                }

    return dict(formatted)


def format_email(data):
    """Apply `data` to templates to format the email body."""
    movies = []

    for title, details in sorted(data.iteritems()):
        entries = []

        for showdate, details in sorted(details.iteritems()):
            day = showdate.strftime("%a")
            image = details["image"]

            entries += [
                Templates.entry.safe_substitute(
                    showdate=day,
                    start=entry["start"].strftime("%H:%M"),
                    ends=entry["end"].strftime("%H:%M"),
                    cinema=entry["cinema"],
                    book=URLs.tickets.format(
                        url=entry["book"]
                    )
                )
                for entry
                in sorted(details["times"], key=lambda x: x["start"])
            ]

        movies.append(
            Templates.movie.safe_substitute(
                title=title,
                image=image,
                times="\n".join(entries)
            )
        )

    return Templates.base.safe_substitute(
        issue_date=date.today().strftime("%Y-%m-%d"),
        movies="\n".join(movies)
    ).encode('ascii', 'xmlcharrefreplace')


def send_message(data):
    return post(
        "https://api.eu.mailgun.net/v3/{}/messages".format(
            CONFIG.get("mailgun", "domain")
        ),
        auth=("api", CONFIG.get("mailgun", "api_key")),
        data={
            "from": CONFIG.get("mailgun", "from"),
            "to": CONFIG.get("mailgun", "recipients").split(","),
            "subject": "This week's movies",
            "html": data
        })


def run():
    today = date.today()
    dates = [
        (today + timedelta(days=inc)).strftime("%d-%m-%Y")
        for inc
        in range(ONE_WEEK)
    ]

    listings = gather_listings(dates)
    data = extract_data(listings)
    reformatted = reformat_data(data)
    updated = add_imdb_details(reformatted)
    result = filter_by_language(updated)

    email = format_email(result)
    send_message(email)


if __name__ == "__main__":
    run()
