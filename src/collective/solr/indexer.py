import os

from logging import getLogger
from Acquisition import aq_get
from DateTime import DateTime
from datetime import date, datetime
from zope.component import getUtility, queryUtility, queryMultiAdapter
from zope.component import queryAdapter, adapts
from zope.interface import implements
from zope.interface import Interface
from zope.contenttype import guess_content_type
from ZODB.POSException import ConflictError
from Products.CMFCore.utils import getToolByName
from Products.CMFCore.CMFCatalogAware import CMFCatalogAware
from Products.Archetypes.CatalogMultiplex import CatalogMultiplex
from Products.Archetypes.interfaces import IBaseObject
from plone.app.content.interfaces import IIndexableObjectWrapper
from plone.indexer.interfaces import IIndexableObject

from collective.solr.interfaces import ISolrConnectionConfig
from collective.solr.interfaces import ISolrConnectionManager
from collective.solr.interfaces import ISolrIndexQueueProcessor
from collective.solr.interfaces import ICheckIndexable
from collective.solr.interfaces import ISolrAddHandler
from collective.solr.solr import SolrException
from collective.solr.utils import prepareData
from socket import error
from urllib import urlencode, quote

from ZODB.POSException import POSKeyError

logger = getLogger('collective.solr.indexer')


class BaseIndexable(object):

    implements(ICheckIndexable)
    adapts(Interface)

    def __init__(self, context):
        self.context = context

    def __call__(self):
        return  isinstance(self.context, CatalogMultiplex) or \
                isinstance(self.context, CMFCatalogAware)


def datehandler(value):
    if value is None:
        raise AttributeError
    if isinstance(value, str) and not value.endswith('Z'):
        value = DateTime(value)

    if isinstance(value, DateTime):
        v = value.toZone('UTC')
        value = '%04d-%02d-%02dT%02d:%02d:%06.3fZ' % (v.year(),
            v.month(), v.day(), v.hour(), v.minute(), v.second())
    elif isinstance(value, datetime):
        # Convert a timezone aware timetuple to a non timezone aware time
        # tuple representing utc time. Does nothing if object is not
        # timezone aware
        value = datetime(*value.utctimetuple()[:7])
        value = '%s.%03dZ' % (value.strftime('%Y-%m-%dT%H:%M:%S'), value.microsecond % 1000)
    elif isinstance(value, date):
        value = '%s.000Z' % value.strftime('%Y-%m-%dT%H:%M:%S')
    return value

def inthandler(value):
    if value is None or value is "":
        raise AttributeError("Solr cant handle none strings or empty values")
    else:
	return value


handlers = {
    'solr.DateField': datehandler,
    'solr.TrieDateField': datehandler,
    'solr.TrieIntField': inthandler,
    'solr.IntField': inthandler,
}

class DefaultAdder(object):
    """
    """

    implements(ISolrAddHandler)
    adapts(IBaseObject)

    def __init__(self, context):
        self.context = context

    def __call__(self, conn, **data):
        # remove in Plone unused field links,
        # which gives problems with some documents
        data.pop('links', '')
        conn.add(**data)

class BinaryAdder(DefaultAdder):
    """
    """

    def getpath(self):
        field = self.context.getPrimaryField()
        blob = field.get(self.context).blob
        return blob._p_blob_committed or blob._p_blob_uncommitted

    def __call__(self, conn, **data):
        if 'ZOPETESTCASE' in os.environ:
            return super(BinaryAdder, self).__call__(conn, **data)
        ignore = ('SearchableText', 'created', 'Type', 'links',
                  'description', 'Date')
        postdata = dict([('literal.%s' % key, val) for key, val in data.iteritems()
                     if key not in ignore])
        portal_state = self.context.restrictedTraverse('@@plone_portal_state')
        postdata['stream.file'] = self.getpath()
        postdata['stream.contentTyp'] = data.get('content_type',
                                                 'application/octet-stream')
        postdata['fmap.content'] = 'SearchableText'
        postdata['extractFormat'] = 'text'

        url = '%s/update/extract' % conn.solrBase

        try:
            conn.doPost(url, urlencode(postdata, doseq=True), conn.formheaders)
            conn.flush()
        except SolrException, e:
            logger.warn('Error %s @ %s', e, data['path_string'])
            conn.reset()

def boost_values(obj, data):
    """ calculate boost values using a method or skin script;  returns
        a dictionary with the values or `None` """
    boost_index_getter = aq_get(obj, 'solr_boost_index_values', None)
    if boost_index_getter is not None:
        return boost_index_getter(data)


class SolrIndexProcessor(object):
    """ a queue processor for solr """
    implements(ISolrIndexQueueProcessor)

    def __init__(self, manager=None):
        self.manager = manager

    def index(self, obj, attributes=None):
        conn = self.getConnection()
        if conn is not None and ICheckIndexable(obj)():
            # unfortunately with current versions of solr we need to provide
            # data for _all_ fields during an <add> -- partial updates aren't
            # supported (see https://issues.apache.org/jira/browse/SOLR-139)
            # however, the reindexing can be skipped if none of the given
            # attributes match existing solr indexes...
            schema = self.manager.getSchema()
            if schema is None:
                msg = 'unable to fetch schema, skipping indexing of %r'
                logger.warning(msg, obj)
                return
            uniqueKey = schema.get('uniqueKey', None)
            if uniqueKey is None:
                msg = 'schema is missing unique key, skipping indexing of %r'
                logger.warning(msg, obj)
                return
            if attributes is not None:
                attributes = set(schema.keys()).intersection(attributes)
                if not attributes:
                    return
            data, missing = self.getData(obj)
            if not data:
                return          # don't index with no data...
            prepareData(data)
            if data.get(uniqueKey, None) is not None and not missing:
                config = getUtility(ISolrConnectionConfig)
                if config.commit_within:
                    data['commitWithin'] = config.commit_within
                try:
                    logger.debug('indexing %r (%r)', obj, data)
                    pt = data.get('portal_type', 'default')
                    logger.debug('indexing %r with %r adder (%r)', obj, pt, data)

                    adder = queryAdapter(obj, ISolrAddHandler, name=pt)
                    
                    if adder is None:
                        adder = DefaultAdder(obj)
                    adder(conn, boost_values=boost_values(obj, data), **data)
                except (SolrException, error):
                    logger.exception('exception during indexing %r', obj)

    def reindex(self, obj, attributes=None):
        self.index(obj, attributes)

    def unindex(self, obj):
        conn = self.getConnection()
        if conn is not None:
            schema = self.manager.getSchema()
            if schema is None:
                msg = 'unable to fetch schema, skipping unindexing of %r'
                logger.warning(msg, obj)
                return
            uniqueKey = schema.get('uniqueKey', None)
            if uniqueKey is None:
                msg = 'schema is missing unique key, skipping unindexing of %r'
                logger.warning(msg, obj)
                return

            # remove the PathWrapper, otherwise IndexableObjectWrapper fails
            # to get the UID indexer (for dexterity objects) and the parent 
            # UID is acquired
            if hasattr(obj, 'context'):
                obj = obj.context

            data, missing = self.getData(obj, attributes=[uniqueKey])
            prepareData(data)
            if not uniqueKey in data:
                msg = 'Can not unindex: no unique key for object %r'
                logger.info(msg, obj)
                return
            data_key = data[uniqueKey]
            if data_key is None:
                msg = 'Can not unindex: `None` unique key for object %r'
                logger.info(msg, obj)
                return
            try:
                logger.debug('unindexing %r (%r)', obj, data)
                conn.delete(id=data_key)
            except (SolrException, error):
                logger.exception('exception during unindexing %r', obj)

    def begin(self):
        pass

    def commit(self, wait=None):
        conn = self.getConnection()
        if conn is not None:
            config = getUtility(ISolrConnectionConfig)
            if not isinstance(wait, bool):
                wait = not config.async
            try:
                logger.debug('committing')
                if not config.auto_commit or config.commit_within:
                    # If we have commitWithin enabled, we never want to do
                    # explicit commits. Even though only add's support this
                    # and we might wait a bit longer on delete's this way
                    conn.flush()
                else:
                    conn.commit(waitFlush=wait, waitSearcher=wait)
            except (SolrException, error):
                logger.exception('exception during commit')
            self.manager.closeConnection()

    def abort(self):
        conn = self.getConnection()
        if conn is not None:
            logger.debug('aborting')
            conn.abort()
            self.manager.closeConnection()

    # helper methods

    def getConnection(self):
        if self.manager is None:
            self.manager = queryUtility(ISolrConnectionManager)
        if self.manager is not None:
            self.manager.setIndexTimeout()
            return self.manager.getConnection()

    def wrapObject(self, obj):
        """ wrap object with an "IndexableObjectWrapper` (for Plone < 3.3) or
            adapt it to `IIndexableObject` (for Plone >= 3.3), see
            `CMFPlone...CatalogTool.catalog_object` for some background """
        wrapper = obj
        # first try the new way, i.e. using `plone.indexer`...
        catalog = getToolByName(obj, 'portal_catalog', None)
        adapter = queryMultiAdapter((obj, catalog), IIndexableObject)
        if adapter is not None:
            wrapper = adapter
        else:       # otherwise try the old way...
            portal = getToolByName(obj, 'portal_url', None)
            if portal is None:
                return obj
            portal = portal.getPortalObject()
            adapter = queryMultiAdapter((obj, portal), IIndexableObjectWrapper)
            if adapter is not None:
                wrapper = adapter
            wft = getToolByName(obj, 'portal_workflow', None)
            if wft is not None:
                wrapper.update(wft.getCatalogVariablesFor(obj))
        return wrapper

    def getData(self, obj, attributes=None):
        schema = self.manager.getSchema()
        if schema is None:
            return {}, ()
        if attributes is None:
            attributes = schema.keys()
        obj = self.wrapObject(obj)
        data, marker = {}, []
        for name in attributes:
            try:
                value = getattr(obj, name)
                if callable(value):
                    value = value()
            except ConflictError:
                raise
            except AttributeError:
                continue
            except Exception:
                logger.exception('Error occured while getting data for '
                    'indexing!')
                continue
            field = schema[name]
            handler = handlers.get(field.class_, None)
            if handler is not None:
                try:
                    value = handler(value)
                except AttributeError:
                    continue
            elif isinstance(value, (list, tuple)) and not field.multiValued:
                separator = getattr(field, 'separator', ' ')
                value = separator.join(value)
            if isinstance(value, str):
                value = unicode(value, 'utf-8', 'ignore').encode('utf-8')
            data[name] = value
        missing = set(schema.requiredFields) - set(data.keys())
        return data, missing
