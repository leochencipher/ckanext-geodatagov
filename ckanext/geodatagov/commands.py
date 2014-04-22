import collections
import os
import sys
import re
import csv
import datetime
import json
import urllib
import lxml.etree
import ckan
import ckan.model as model
import ckan.logic as logic
import ckan.lib.search as search
import ckan.logic.schema as schema
import ckan.lib.cli as cli
import requests
import ckanext.harvest.model as harvest_model
import xml.etree.ElementTree as ET
import ckan.lib.munge as munge
import ckan.plugins as p
from ckanext.geodatagov.harvesters.arcgis import _slugify
from pylons import config
from urllib2 import Request, urlopen, URLError, HTTPError
import time
import math

import logging
log = logging.getLogger()

class GeoGovCommand(cli.CkanCommand):
    '''
    Commands:

        paster geodatagov import-harvest-source <harvest_source_data> -c <config>
        paster geodatagov import-orgs <data> -c <config>
        paster geodatagov post-install-dbinit -c <config>
        paster geodatagov import-dms -c <config>
        paster geodatagov clean-deleted -c <config>
    '''
    summary = __doc__.split('\n')[0]
    usage = __doc__

    def command(self):
        '''
        Parse command line arguments and call appropriate method.
        '''
        if not self.args or self.args[0] in ['--help', '-h', 'help']:
            print GeoGovCommand.__doc__
            return

        cmd = self.args[0]
        self._load_config()

        user = logic.get_action('get_site_user')(
            {'model': model, 'ignore_auth': True}, {}
        )
        self.user_name = user['name']

        if cmd == 'import-harvest-source':
            if not len(self.args) in [2]:
                print GeoGovCommand.__doc__
                return

            self.import_harvest_source(self.args[1])

        if cmd == 'import-orgs':
            if not len(self.args) in [2, 3]:
                print GeoGovCommand.__doc__
                return

            self.import_organizations(self.args[1])
        if cmd == 'import-dms':
            if not len(self.args) in [2]:
                print GeoGovCommand.__doc__
                return
            self.import_dms(self.args[1])
        if cmd == 'post-install-dbinit':
            f = open('/usr/lib/ckan/src/ckanext-geodatagov/what_to_alter.sql')
            print "running what_to_alter.sql"
            test = model.Session.execute(f.read())
            f = open('/usr/lib/ckan/src/ckanext-geodatagov/constraints.sql')
            print "running constraints.sql"
            test = model.Session.execute(f.read())
            model.Session.commit()
            print "Success"
        if cmd == 'clean-deleted':
            self.clean_deleted()
        if cmd == 'db_solr_sync':
		    self.db_solr_sync()

    def get_user_org_mapping(self, location):
        user_org_mapping = open(location)
        fields = ['user', 'org']
        csv_reader = csv.reader(user_org_mapping)
        mapping = {}
        for row in csv_reader:
            mapping[row[0].lower()] = row[1]
        return mapping


    def import_harvest_source(self, sources_location):
        '''Import data from this mysql command
select DOCUUID, TITLE, OWNER, APPROVALSTATUS, HOST_URL, Protocol, PROTOCOL_TYPE, FREQUENCY, USERNAME into outfile '/tmp/results_with_user.csv' from GPT_RESOURCE join GPT_USER on owner = USERID where frequency is not null;
'''
        error_log = file('harvest_source_import_errors.txt' , 'w+')

        fields = ['DOCUUID', 'TITLE', 'OWNER', 'APPROVALSTATUS', 'HOST_URL',
        'PROTOCAL', 'PROTOCOL_TYPE', 'FREQUENCY', 'ORGID']

        user = logic.get_action('get_site_user')({'model': model, 'ignore_auth': True}, {})

        harvest_sources = open(sources_location)
        try:
            csv_reader = csv.reader(harvest_sources)
            for row in csv_reader:
                row = dict(zip(fields,row))

                ## neeeds some fix
                #if row['PROTOCOL_TYPE'].lower() not in ('waf', 'csw', 'z3950'):
                    #continue

                #frequency = row['FREQUENCY'].upper()
                #if frequency not in ('WEEKLY', 'MONTHLY', 'BIWEEKLY'):

                frequency = 'MANUAL'

                config = {
                          'ORIGINAL_UUID': row['DOCUUID'][1:-1].lower()
                         }

                protocal = row['PROTOCAL']
                protocal = protocal[protocal.find('<protocol'):]
                import re
                protocal = re.sub('<protocol.*?>', '<protocol>', protocal)

                root = ET.fromstring(protocal[protocal.find('<protocol'):])


                for child in root:
                    if child.text:
                        config[child.tag] = child.text

                harvest_source_dict = {
                    'name': munge.munge_title_to_name(row['TITLE']),
                    'title': row['TITLE'],
                    'url': row['HOST_URL'],
                    'source_type': row['PROTOCOL_TYPE'].lower(),
                    'frequency': frequency,
                    'config': json.dumps(config),
                    'owner_org': row['ORGID']
                }
                harvest_source_dict.update(config)

                try:
                    harvest_source = logic.get_action('harvest_source_create')(
                        {'model': model, 'user': user['name'],
                         'session': model.Session, 'api_version': 3},
                        harvest_source_dict
                    )
                except ckan.logic.ValidationError, e:
                    error_log.write(json.dumps(harvest_source_dict))
                    error_log.write(str(e))
                    error_log.write('\n')

        finally:
            model.Session.commit()
            harvest_sources.close()
            error_log.close()

    def import_organizations(self, location):
        fields = ['title', 'type', 'name']

        user = logic.get_action('get_site_user')({'model': model, 'ignore_auth': True}, {})
        organizations = open(location)

        csv_reader = csv.reader(organizations)

        all_rows = set()
        for row in csv_reader:
            all_rows.add(tuple(row))

        for num, row in enumerate(all_rows):
            row = dict(zip(fields,row))
            org = logic.get_action('organization_create')(
                {'model': model, 'user': user['name'],
                 'session': model.Session},
                {'name': row['name'],
                 'title': row['title'],
                 'extras': [{'key': 'organization_type',
                             'value': row['type']}]
                }
            )


    def import_dms(self, url):

        input_records = requests.get(url).json()
        to_import = {}
        for record in input_records:
            to_import[record['identifier']] = record

        user = logic.get_action('get_site_user')(
            {'model': model, 'ignore_auth': True}, {}
        )

        collected_ids = set(to_import.keys())

        existing_package_ids = set([row[0] for row in
                       model.Session.query(model.Package.id).from_statement(
                           '''select p.id
                           from package p
                           join package_extra pe on p.id = pe.package_id
                           where pe.key = 'metadata-source' and pe.value = 'dms'
                           and p.state = 'active' ''')])

        context = {}
        context['user'] = self.user_name

        for num, package_id in enumerate(collected_ids - existing_package_ids):
            context.pop('package', None)
            context.pop('group', None)
            new_package = to_import[package_id]
            try:
                print str(datetime.datetime.now()) + ' Created id ' + package_id
                logic.get_action('datajson_create')(context, new_package)
            except Exception, e:
                print str(datetime.datetime.now()) + ' Error when creating id ' + package_id
                print e

        for package_id in collected_ids & existing_package_ids:
            context.pop('package', None)
            context.pop('group', None)
            new_package = to_import[package_id]
            try:
                logic.get_action('datajson_update')(context, new_package)
            except Exception, e:
                print str(datetime.datetime.now()) + ' Error when updating id ' + package_id
                print e
        for package_id in existing_package_ids - collected_ids:
            context.pop('package', None)
            context.pop('group', None)
            try:
                logic.get_action('package_delete')(context, {"id":package_id})
            except Exception, e:
                print str(datetime.datetime.now()) + ' Error when deleting id ' + package_id
                print e


    def clean_deleted(self):
        print str(datetime.datetime.now()) + ' Starting delete'
        sql = '''begin; update package set state = 'to_delete' where state <> 'active' and revision_id in (select id from revision where timestamp < now() - interval '1 day');
        update package set state = 'to_delete' where owner_org is null;
        delete from package_role where package_id in (select id from package where state = 'to_delete' );
        delete from user_object_role where id not in (select user_object_role_id from package_role) and context = 'Package';
        delete from resource_revision where resource_group_id in (select id from resource_group where package_id in (select id from package where state = 'to_delete'));
        delete from resource_group_revision where package_id in (select id from package where state = 'to_delete');
        delete from package_tag_revision where package_id in (select id from package where state = 'to_delete');
        delete from member_revision where table_id in (select id from package where state = 'to_delete');
        delete from package_extra_revision where package_id in (select id from package where state = 'to_delete');
        delete from package_revision where id in (select id from package where state = 'to_delete');
        delete from package_tag where package_id in (select id from package where state = 'to_delete');
        delete from resource where resource_group_id in (select id from resource_group where package_id in (select id from package where state = 'to_delete'));
        delete from package_extra where package_id in (select id from package where state = 'to_delete');
        delete from member where table_id in (select id from package where state = 'to_delete');
        delete from resource_group where package_id  in (select id from package where state = 'to_delete');

        delete from harvest_object_error hoe using harvest_object ho where ho.id = hoe.harvest_object_id and package_id  in (select id from package where state = 'to_delete');
        delete from harvest_object_extra hoe using harvest_object ho where ho.id = hoe.harvest_object_id and package_id  in (select id from package where state = 'to_delete');
        delete from harvest_object where package_id in (select id from package where state = 'to_delete');

        delete from package where id in (select id from package where state = 'to_delete'); commit;'''
        model.Session.execute(sql)
        print str(datetime.datetime.now()) + ' Finished delete'

#set([u'feed', u'webService', u'issued', u'modified', u'references', u'keyword', u'size', u'landingPage', u'title', u'temporal', u'theme', u'spatial', u'dataDictionary', u'description', u'format', u'granularity', u'accessLevel', u'accessURL', u'publisher', u'language', u'license', u'systemOfRecords', u'person', u'accrualPeriodicity', u'dataQuality', u'distribution', u'identifier', u'mbox'])


#{u'title': 6061, u'theme': 6061, u'accessLevel': 6061, u'publisher': 6061, u'identifier': 6061, u'description': 6060, u'accessURL': 6060, u'distribution': 6060, u'keyword': 6059, u'person': 6057, u'accrualPeriodicity': 6056, u'format': 6047, u'spatial': 6009, u'size': 5964, u'references': 5841, u'dataDictionary': 5841, u'temporal': 5830, u'modified': 5809, u'issued': 5793, u'mbox': 5547, u'granularity': 4434, u'license': 2048, u'dataQuality': 453}


    def db_solr_sync(self):

        print str(datetime.datetime.now()) + ' Entering Database Solr Sync function.'

        url = config.get('solr_url') + "/select?q=*%3A*&sort=id+asc&fl=id%2Cmetadata_modified&wt=json&indent=true"
        response = get_response(url)
    
        if (response != 'error'):

          print str(datetime.datetime.now()) + ' Deleting records from solr_pkg_ids.'		
          sql = '''delete from solr_pkg_ids'''
          model.Session.execute(sql)
          model.Session.commit()
		
          f = response.read()
          data = json.loads(f)
          rows = data.get('response').get('numFound')

          start = 0
          chunk_size = 1000         

          print str(datetime.datetime.now()) + ' Starting insertion of records in solr_pkg_ids .'
 
          for x in range(0, int(math.ceil(rows/chunk_size))+1):
		  
            if(x == 0):
               start = 0
			
            print str(datetime.datetime.now()) + ' Fetching ' + url + "&rows=" + str(chunk_size) + "&start=" + str(start)			  
			  
            response = get_response(url + "&rows=" + str(chunk_size) + "&start=" + str(start))
            f = response.read()
            data = json.loads(f)
            results = data.get('response').get('docs')

            print str(datetime.datetime.now()) + ' Inserting ' + str(start) + ' - ' + str(start + int(data.get('responseHeader').get('params').get('rows')) - 1) + ' of ' + str(rows)			
			
            for x in range(0, len(results)):
                sql = '''select count(id) as count from package where id = :pkg_id;'''
                q = model.Session.execute(sql, {'pkg_id' : results[x]['id']})            
                for row in q:
                   if(row['count'] == 0):
                     sql = '''insert into solr_pkg_ids (pkg_id, action) values (:pkg_id, :action);'''
                     model.Session.execute(sql, {'pkg_id' : results[x]['id'], 'action' : 'notfound' })
                     model.Session.commit()			
                   else:
                     sql = '''select replace(to_char(ts, 'YYYY-MM-DDT HH24:MI:SS.MS'), ' ', '') || 'Z' as modified_dt from 
                        (select timestamp as ts from package p 
                        join revision r on p.revision_id = r.id
                        where p.id = :pkg_id
                        union all
                        select revision_timestamp as ts from package p
                        join package_revision prv on prv.id = p.id and prv.current = 't' and prv.state = 'active'
                        where p.id = :pkg_id
                        union all
                        select timestamp as ts from package p 
                        join package_extra pe on pe.package_id = p.id
                        join revision r on pe.revision_id = r.id
                        where p.id = :pkg_id
                        union all
                        select revision_timestamp as ts from package p 
                        join package_extra_revision per on per.package_id = p.id and per.current = 't' and per.state = 'active'
                        where p.id = :pkg_id
                        union all
                        select timestamp as ts from package p 
                        join package_relationship pr on pr.subject_package_id = p.id
                        join revision r on pr.revision_id = r.id
                        where p.id = :pkg_id
                        union all
                        select revision_timestamp as ts from package p 
                        join package_relationship_revision prr on prr.subject_package_id = p.id and prr.current = 't' and prr.state = 'active'
                        where p.id = :pkg_id
                        union all
                        select timestamp as ts from package p 
                        join package_relationship pr2 on pr2.object_package_id = p.id
                        join revision r on pr2.revision_id = r.id
                        where p.id = :pkg_id
                        union all
                        select revision_timestamp as ts from package p 
                        join package_relationship_revision prr1 on prr1.object_package_id = p.id and prr1.current = 't' and prr1.state = 'active'
                        where p.id = :pkg_id
                        union all
                        select timestamp as ts from package p 
                        join resource_group rg on rg.package_id = p.id
                        join revision r on rg.revision_id = r.id
                        where p.id = :pkg_id
                        union all
                        select revision_timestamp as ts from package p 
                        join resource_group_revision rgr on rgr.package_id = p.id and rgr.current = 't' and rgr.state = 'active'
                        where p.id = :pkg_id
                        union all
                        select timestamp as ts from package p 
                        join resource_group rg on rg.package_id = p.id
                        join resource rs on rs.resource_group_id = rg.id
                        join revision r on rs.revision_id = r.id
                        where p.id = :pkg_id
                        union all
                        select revision_timestamp as ts from package p 
                        join resource_group rg1 on rg1.package_id = p.id 
                        join resource_revision rr on rr.resource_group_id = rg1.id and rr.current = 't' and rr.state = 'active'
                        where p.id = :pkg_id
                        union all
                        select timestamp as ts from package p 
                        join package_tag pt on  pt.package_id = p.id
                        join revision r on pt.revision_id = r.id
                        where p.id = :pkg_id
                        union all
                        select revision_timestamp as ts from package p 
                        join package_tag_revision ptr on ptr.package_id = p.id and ptr.current = 't' and ptr.state = 'active'
                        where p.id = :pkg_id
                        union all
                        select timestamp as ts from package p
                        join group_extra ge on ge.group_id = p.owner_org
                        join revision r on r.id = ge.revision_id
                        where p.id = :pkg_id
                        union all
                        select revision_timestamp as ts from package p 
                        join group_extra_revision ger on ger.group_id = p.owner_org and ger.current = 't' and ger.state = 'active' 
                        where p.id = :pkg_id
                        union all
                        select timestamp  as ts from package p
                        join public.group g on g.id = p.owner_org
                        join revision r on r.id = g.revision_id
                        where p.id = :pkg_id
                        union all
                        select revision_timestamp as ts from package p 
                        join group_revision gr on gr.id = p.owner_org and gr.current = 't' and gr.state = 'active'
                        where p.id = :pkg_id) temp 
                        order by ts desc limit 1;'''
                    
                     q1 = model.Session.execute(sql, {'pkg_id' : results[x]['id']})      
                     for row1 in q1:					                        			 
               
                       if(str(results[x]['metadata_modified'])[:19] != str(row1['modified_dt'])[:19]):
                         print str(datetime.datetime.now()) + ' Action Type : outsync for Package Id: ' + results[x]['id']               
                         print str(datetime.datetime.now()) + ' Modified Date from Solr: ' + str(results[x]['metadata_modified'])
                         print str(datetime.datetime.now()) + ' Modified Date from Db: ' + str(row1['modified_dt'])
                         sql = '''insert into solr_pkg_ids (pkg_id, action) values (:pkg_id, :action);'''  
                         model.Session.execute(sql, {'pkg_id' : results[x]['id'], 'action' : 'outsync' })      
                         model.Session.commit()
                       else:
                         sql = '''insert into solr_pkg_ids (pkg_id, action) values (:pkg_id, :action);'''  
                         model.Session.execute(sql, {'pkg_id' : results[x]['id'], 'action' : 'insync' })      
                         model.Session.commit()
                     
            start = int(data.get('responseHeader').get('params').get('start')) + chunk_size			       
          
          print str(datetime.datetime.now()) + ' Starting Database to Solr Sync'           
          
          sql = '''Select id from package where id not in (select pkg_id from solr_pkg_ids); '''
          q = model.Session.execute(sql)
          for row in q:
            try:
              print str(datetime.datetime.now()) + ' Building Id: ' + row['id']
              search.rebuild(row['id'])
            except ckan.logic.NotFound:
              print "Error: Not Found."
            except KeyboardInterrupt:
              print "Stopped."
              return
            except:
              raise
          
          sql = '''Select pkg_id from solr_pkg_ids where action = 'outsync'; '''
          q = model.Session.execute(sql)          
          for row in q:
            try:
              print str(datetime.datetime.now()) + ' Rebuilding Id: ' + row['id']
              search.rebuild(row['id'])
            except ckan.logic.NotFound:
              print "Error: Not Found."
            except KeyboardInterrupt:
              print "Stopped."
              return
            except:
              raise
          
          print str(datetime.datetime.now()) + ' Starting Solr to Database Sync'
          
          sql = '''Select pkg_id from solr_pkg_ids where action = 'notfound'; '''
          q = model.Session.execute(sql)
          for row in q:
            try:
              search.clear(row['pkg_id'])
            except ckan.logic.NotFound:
              print "Error: Not Found."
            except KeyboardInterrupt:
              print "Stopped."
              return
            except:
              raise
          
          print str(datetime.datetime.now()) + " All Sync Done."

def get_response(url):
    req = Request(url)
    try:
      response = urlopen(req)
    except HTTPError as e:
      print 'The server couldn\'t fulfill the request.'
      print 'Error code: ', e.code
      return 'error'
    except URLError as e:
      print 'We failed to reach a server.'
      print 'Reason: ', e.reason
      return 'error'
    else:
      return response