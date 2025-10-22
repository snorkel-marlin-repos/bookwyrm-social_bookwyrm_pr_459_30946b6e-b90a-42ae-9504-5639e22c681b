''' functionality outline for a book data connector '''
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
import logging
from urllib3.exceptions import RequestError

from django.db import transaction
import requests
from requests import HTTPError
from requests.exceptions import SSLError

from bookwyrm import activitypub, models, settings


logger = logging.getLogger(__name__)
class ConnectorException(HTTPError):
    ''' when the connector can't do what was asked '''


class AbstractMinimalConnector(ABC):
    ''' just the bare bones, for other bookwyrm instances '''
    def __init__(self, identifier):
        # load connector settings
        info = models.Connector.objects.get(identifier=identifier)
        self.connector = info

        # the things in the connector model to copy over
        self_fields = [
            'base_url',
            'books_url',
            'covers_url',
            'search_url',
            'max_query_count',
            'name',
            'identifier',
            'local'
        ]
        for field in self_fields:
            setattr(self, field, getattr(info, field))

    def search(self, query, min_confidence=None):# pylint: disable=unused-argument
        ''' free text search '''
        resp = requests.get(
            '%s%s' % (self.search_url, query),
            headers={
                'Accept': 'application/json; charset=utf-8',
                'User-Agent': settings.USER_AGENT,
            },
        )
        if not resp.ok:
            resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError as e:
            logger.exception(e)
            raise ConnectorException('Unable to parse json response', e)
        results = []

        for doc in self.parse_search_data(data)[:10]:
            results.append(self.format_search_result(doc))
        return results

    @abstractmethod
    def get_or_create_book(self, remote_id):
        ''' pull up a book record by whatever means possible '''

    @abstractmethod
    def parse_search_data(self, data):
        ''' turn the result json from a search into a list '''

    @abstractmethod
    def format_search_result(self, search_result):
        ''' create a SearchResult obj from json '''


class AbstractConnector(AbstractMinimalConnector):
    ''' generic book data connector '''
    def __init__(self, identifier):
        super().__init__(identifier)
        # fields we want to look for in book data to copy over
        # title we handle separately.
        self.book_mappings = []


    def is_available(self):
        ''' check if you're allowed to use this connector '''
        if self.max_query_count is not None:
            if self.connector.query_count >= self.max_query_count:
                return False
        return True


    @transaction.atomic
    def get_or_create_book(self, remote_id):
        ''' translate arbitrary json into an Activitypub dataclass '''
        # first, check if we have the origin_id saved
        existing = models.Edition.find_existing_by_remote_id(remote_id) or \
                models.Work.find_existing_by_remote_id(remote_id)
        if existing:
            if hasattr(existing, 'get_default_editon'):
                return existing.get_default_editon()
            return existing

        # load the json
        data = get_data(remote_id)
        mapped_data = dict_from_mappings(data, self.book_mappings)
        if self.is_work_data(data):
            try:
                edition_data = self.get_edition_from_work_data(data)
            except KeyError:
                # hack: re-use the work data as the edition data
                # this is why remote ids aren't necessarily unique
                edition_data = data
            work_data = mapped_data
        else:
            try:
                work_data = self.get_work_from_edition_data(data)
                work_data = dict_from_mappings(work_data, self.book_mappings)
            except KeyError:
                work_data = mapped_data
            edition_data = data

        if not work_data or not edition_data:
            raise ConnectorException('Unable to load book data: %s' % remote_id)

        # create activitypub object
        work_activity = activitypub.Work(**work_data)
        # this will dedupe automatically
        work = work_activity.to_model(models.Work)
        for author in self.get_authors_from_data(data):
            work.authors.add(author)
        return self.create_edition_from_data(work, edition_data)


    def create_edition_from_data(self, work, edition_data):
        ''' if we already have the work, we're ready '''
        mapped_data = dict_from_mappings(edition_data, self.book_mappings)
        mapped_data['work'] = work.remote_id
        edition_activity = activitypub.Edition(**mapped_data)
        edition = edition_activity.to_model(models.Edition)
        edition.connector = self.connector
        edition.save()

        work.default_edition = edition
        work.save()

        for author in self.get_authors_from_data(edition_data):
            edition.authors.add(author)
        if not edition.authors.exists() and work.authors.exists():
            edition.authors.set(work.authors.all())

        return edition


    def get_or_create_author(self, remote_id):
        ''' load that author '''
        existing = models.Author.find_existing_by_remote_id(remote_id)
        if existing:
            return existing

        data = get_data(remote_id)

        mapped_data = dict_from_mappings(data, self.author_mappings)
        activity = activitypub.Author(**mapped_data)
        # this will dedupe
        return activity.to_model(models.Author)


    @abstractmethod
    def is_work_data(self, data):
        ''' differentiate works and editions '''

    @abstractmethod
    def get_edition_from_work_data(self, data):
        ''' every work needs at least one edition '''

    @abstractmethod
    def get_work_from_edition_data(self, data):
        ''' every edition needs a work '''

    @abstractmethod
    def get_authors_from_data(self, data):
        ''' load author data '''

    @abstractmethod
    def expand_book_data(self, book):
        ''' get more info on a book '''


def dict_from_mappings(data, mappings):
    ''' create a dict in Activitypub format, using mappings supplies by
    the subclass '''
    result = {}
    for mapping in mappings:
        result[mapping.local_field] = mapping.get_value(data)
    return result


def get_data(url):
    ''' wrapper for request.get '''
    try:
        resp = requests.get(
            url,
            headers={
                'Accept': 'application/json; charset=utf-8',
                'User-Agent': settings.USER_AGENT,
            },
        )
    except RequestError:
        raise ConnectorException()
    if not resp.ok:
        resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        raise ConnectorException()

    return data


def get_image(url):
    ''' wrapper for requesting an image '''
    try:
        resp = requests.get(
            url,
            headers={
                'User-Agent': settings.USER_AGENT,
            },
        )
    except (RequestError, SSLError):
        return None
    if not resp.ok:
        return None
    return resp


@dataclass
class SearchResult:
    ''' standardized search result object '''
    title: str
    key: str
    author: str
    year: str
    connector: object
    confidence: int = 1

    def __repr__(self):
        return "<SearchResult key={!r} title={!r} author={!r}>".format(
            self.key, self.title, self.author)

    def json(self):
        ''' serialize a connector for json response '''
        serialized = asdict(self)
        del serialized['connector']
        return serialized


class Mapping:
    ''' associate a local database field with a field in an external dataset '''
    def __init__(self, local_field, remote_field=None, formatter=None):
        noop = lambda x: x

        self.local_field = local_field
        self.remote_field = remote_field or local_field
        self.formatter = formatter or noop

    def get_value(self, data):
        ''' pull a field from incoming json and return the formatted version '''
        value = data.get(self.remote_field)
        if not value:
            return None
        try:
            return self.formatter(value)
        except:# pylint: disable=bare-except
            return None
