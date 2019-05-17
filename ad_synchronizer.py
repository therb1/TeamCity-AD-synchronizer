import argparse
import configparser
import re
from ldap3 import Server, Connection, ALL
from urllib.parse import urlparse
import requests
from string import ascii_letters, digits
import random
from lxml import etree
import json
from threading import Event
from loginit import initLog
from diskcache import Cache
import time

def get_args():
    def _usage():
        return """
    Usage: teamcity-ldap-sync -f <config>
           teamcity-ldap-sync -h

    Options:
      -h, --help                    Display this usage info
      -f <config>, --file <config>  Configuration file to use

    """

    """Get command line args from the user"""
    parser = argparse.ArgumentParser(description="Standard Arguments", usage=_usage())

    parser.add_argument("-f", "--file",
                        required=True,
                        help="Configuration file to use")

    args = parser.parse_args()

    return args

class createConfig(object):
    """
    TeamCity AD synchronizer configuration parse class
    Provides methods for parsing and retrieving config entries
    """
    def __init__(self, parser):
        try:
                self.sync_interval = parser.get('common', 'sync_interval')
                self.ldap_uri = parser.get('ldap', 'uri')
                self.ldap_user = parser.get('ldap', 'binduser')
                self.ldap_pass = parser.get('ldap', 'bindpass')
                self.groups_search_scope = parser.get('ldap', 'groups_search_scope')
                try:
                    self.groups_search_base_list = re.findall(r"['\"](.*?)['\"]", parser.get('ldap', 'groups_search_base_list'))
                    self.custom_groups_list = re.findall(r"['\"](.*?)['\"]", parser.get('ldap', 'custom_groups_list'))
                except:
                    pass

                self.tc_server = parser.get('teamcity', 'server')
                self.tc_username = parser.get('teamcity', 'username')
                self.tc_password = parser.get('teamcity', 'password')
                self.tc_verify_certificate = parser.get('teamcity', 'verify_certificate')
                self.tc_timeout = int(parser.get('teamcity', 'timeout'))

                self.path_ldap_mapping = parser.get('xml', 'path_ldap_mapping')

                self.ad_cache_file = parser.get('ad', 'cache_file')
                self.ad_cache_ttl = int(parser.get('ad', 'cache_ttl'))

        except configparser.NoOptionError as e:
            raise SystemExit('Configuration issues detected in %s' % e)

class LDAPConnector(object):
    """
    LDAP connector class
    Defines methods for retrieving groups from LDAP server.
    """

    def __init__(self, args, config):
        self.uri = urlparse(config.ldap_uri)
        self.ldap_user = config.ldap_user
        self.ldap_pass = config.ldap_pass

    def __enter__(self):
        server = Server(host=self.uri.hostname,
                        port=self.uri.port,
                        get_info=ALL)

        self.conn = Connection(server=server,
                               user=self.ldap_user,
                               password=self.ldap_pass,
                               check_names=True,
                               raise_exceptions=True)

        self.conn.bind()

        return self

    def __exit__(self, exctype, exception, traceback):
        self.conn.unbind()

    def search_groups(self, search_base, search_scope):
        """
        Retrieves list of groups by distinguishedName attribute
        Args:
            :param search_base: List of LDAP distinguished names to lookup
        Returns:
            DistinguishedName attribute of group list
        """
        ldap_group_list = list()
        for dn in search_base:
            self.conn.search(search_base=dn,
                             search_filter='(objectclass=group)',
                             attributes=["distinguishedName"],
                             search_scope=search_scope)

            for e in self.conn.entries:
                ldap_group_list.append(e.distinguishedName[0])
        return ldap_group_list

class TeamCityClient(object):
    def __init__(self, config, local_cache):
        self.rest_url = '{url}/app/rest/'.format(url=config.tc_server)
        self.session = requests.Session()
        self.session.auth = (config.tc_username, config.tc_password)
        self.session.headers.update({'Content-type': 'application/json', 'Accept': 'application/json', 'Origin': config.tc_server})
        self.session.verify = config.tc_verify_certificate
        self.timeout = config.tc_timeout
        self.cache = Cache(config.ad_cache_file)
        self.cache_ttl = config.ad_cache_ttl

    def get_tc_groups(self):
        url = self.rest_url + 'userGroups'
        groups_in_tc = self.session.get(url, verify=self.session.verify, timeout=self.timeout)
        if groups_in_tc.status_code == 200:
            groups_in_tc = groups_in_tc.json()
            log.debug("Get group list was successfully from TC")
        else:
            log.error("Couldn't get group list from TC. Responce code: {}".format(groups_in_tc.status_code))
            raise Exception('Teamcity error')
        group_list = [group for group in groups_in_tc['group']]
        renamed_group_list=list()
        for group in group_list:
            renamed_group_list.append({'teamcityGroupKey': group.get("key"), 'name': group.get("name")})

        return renamed_group_list

    def create_groups(self, groups_list):
        ldap_list_with_keys = list()
        for group_obj in groups_list:
            if group_obj.get("ldapGroupDn"):
                log.info("Creating group {} in TC".format(group_obj.get("name")))
                url = self.rest_url + 'userGroups'
                key = ''.join(random.choice("{}{}".format(ascii_letters, digits)) for i in range(16))
                data = json.dumps({"name": group_obj.get("name"), "key": key})
                resp = self.session.post(url, verify=self.session.verify, data=data, timeout=self.timeout)
                if resp.status_code == 200:
                    self.tc_groups = TeamCityClient.get_tc_groups(self)
                    log.info("The group {} was created successfully in TC".format(group_obj.get("name")))
                    self.cache.set(group_obj.get("name"),{'ldapGroupDn': group_obj.get("ldapGroupDn"),
                                                          'name': group_obj.get("name"),
                                                          'teamcityGroupKey': key,
                                                          'cache_state': 'created'}, expire=self.cache_ttl)
                    self.cache.close()
                    ldap_list_with_keys.append({'ldapGroupDn': group_obj.get("ldapGroupDn"),
                                                          'name': group_obj.get("name"),
                                                          'teamcityGroupKey': key,
                                                          'cache_state': 'created'})

                else:
                    log.error("Couldn't create group {} in TC \n{}".format(group_obj.get("name"), resp.content))
                    raise Exception('Teamcity error')
        return True

    def delete_groups(self, groups_list):
        for group_obj in groups_list:
            url = '{url}userGroups/key:{key}'.format(url=self.rest_url, key=group_obj.get('key'))
            resp = self.session.delete(url, verify=False)
            group_name = re.findall(r"CN\=(.*?),", group_obj.get("ldapGroupDn"))[0]
            if resp.status_code == 204:
                log.info("The group {} was deleted successfully from TC".format(group_name))
                self.cache.set(group_name, {'ldapGroupDn': group_obj.get("ldapGroupDn"),
                                'name': group_name,
                                'teamcityGroupKey': group_obj.get('key'),
                                'cache_state': 'deleted'},expire=self.cache_ttl)
                self.cache.close()
                return True
            else:
                log.error("Couldn't delete group {}\n{}".format(group_obj.get("ldapGroupDn"), resp.content))
                return False


class services(object):
    def reformat_ldap_group_list(self,ldap_group_list):
        formatted_ldap_group_list = list()
        for group in list(ldap_group_list):
            group_name = re.findall(r"CN\=(.*?),", group)[0]
            formatted_ldap_group_list.append({"ldapGroupDn": group, "name": group_name, 'cache_state': None})
        return formatted_ldap_group_list

    def diff_ldap_teamcity_groups(self, formatted_ldap_group_list, teamcity_group_list):
        teamcity_list = [group.get("name") for group in teamcity_group_list]
        diff_groups = list()
        for ldap_group in formatted_ldap_group_list:
                if ldap_group.get("name") not in teamcity_list:
                    diff_groups.append(ldap_group)
        return diff_groups

    def sim_ldap_teamcity_groups(self, ldap_group_list, teamcity_group_list, teamcity_new_groups):
        teamcity_list = [group_obj.get("name") for group_obj in teamcity_new_groups]
        sim_groups = list()
        for group in teamcity_group_list:
            for ldap_group in ldap_group_list:
                if ldap_group.get("name") in group.get("name") and ldap_group.get("name") in teamcity_list:
                    ldap_group_prop = {"name": ldap_group.get("name"),
                                       "ldapGroupDn": ldap_group.get("ldapGroupDn"),
                                       "teamcityGroupKey": group.get("teamcityGroupKey"),
                                       "cache_state": ldap_group.get("cache_state")}
                    sim_groups.append(ldap_group_prop)
                elif ldap_group.get("name") in group.get("name"):
                    ldap_group_prop = {"name": ldap_group.get("name"),
                                       "ldapGroupDn": ldap_group.get("ldapGroupDn"),
                                       "teamcityGroupKey": group.get("teamcityGroupKey"),
                                       "cache_state": "created"}
                    sim_groups.append(ldap_group_prop)
        return sim_groups

    def diff_xml_ldap_groups(self, xml_group_list, ldap_new_groups):
        ldap_group_name_list = [group.get("ldapGroupDn") for group in ldap_new_groups]
        diff_groups = list()
        for xml_group_obj in xml_group_list:
            if xml_group_obj.get('ldapGroupDn') not in ldap_group_name_list:
                diff_groups.append({'ldapGroupDn': xml_group_obj.get('ldapGroupDn'),
                                    'key': xml_group_obj.get('teamcityGroupKey')})
        return diff_groups

class localCache(object):
    def __init__(self, config):
        self.cache_file = config.ad_cache_file

    def __enter__(self):
        self.cache = Cache(self.cache_file)
        self.cache.expire()
        return self

    def __exit__(self, exctype, exception, traceback):
        self.cache.close()

    def correct_ldap_group_list(self, group_list):
        # DELETE just deleted group from list
        deleted_groups = list()
        if len(self.cache) > 0:

            for group in group_list:
                if group.get("name") in self.cache and self.cache.get(group.get("name")).get("cache_state") == "deleted":
                    log.info('Group{0} in state "deleted" founded in cache'.format(group.get("name")))
                    deleted_groups.append(group)

        corrected_group_list = [x for x in group_list if x not in deleted_groups]

        # ADD just created group to list
        created_groups = list()
        groups_name_list = [group.get("name") for group in group_list]
        if len(self.cache) > 0:

            cached = self.cache._sql('SELECT key FROM Cache').fetchall()

            for group in cached:
                if self.cache.get(group[0]).get("name") not in groups_name_list and\
                                self.cache.get(group[0]).get("cache_state") == "created":
                    log.info('Group{0} in state "created" founded in cache'.format(group[0]))
                    created_groups.append(self.cache.get(group[0]))

        corrected_group_list.extend([x for x in created_groups if x not in groups_name_list])

        return corrected_group_list

class xml_changer(object):
    """
    xml_changer configuration class for ldap-mapping.xml
    Provides methods for parsing and retrieving config entries
    """
    def __init__(self, config):
        self.path_ldap_mapping = config.path_ldap_mapping

    def get_current_groups(self):
        parser = etree.XMLParser(remove_blank_text=True)
        out_tree = etree.parse(self.path_ldap_mapping, parser)
        out_root = out_tree.getroot()
        current_xml_list = list()
        for elt in out_root.findall(".//group-mapping"):
            current_xml_list.append(elt.attrib)
        return current_xml_list

    def reganerate_ldap_xml(self, new_groups_list):
        root = etree.Element("mapping")
        for group in new_groups_list:
            root.append( etree.Element("group-mapping", attrib={'teamcityGroupKey': group.get('teamcityGroupKey'), 'ldapGroupDn': group.get('ldapGroupDn')}))
        my_tree = etree.ElementTree(root)
        my_tree.write(self.path_ldap_mapping, pretty_print=True, doctype='<!DOCTYPE mapping SYSTEM "ldap-mapping.dtd">')



def main():

    #Start application
    log.info("Run Synchronizer")

    # Parse CLI arguments
    args = get_args()

    # Read config file
    parser = configparser.RawConfigParser()
    parser.read(args.file)

    # Create config object from config file
    config = createConfig(parser)

    # Interval between syncs
    time.sleep(int(config.sync_interval))

    # Connect to LDAP
    with LDAPConnector(args, config) as ldap_conn:
        ldap_group_list = ldap_conn.search_groups(config.groups_search_base_list,config.groups_search_scope)
        #Add custom groups from config
        if config.custom_groups_list:
            ldap_group_list = ldap_group_list + config.custom_groups_list
        #Check ldap groups and/or ldap custom groups is set
        if len(ldap_group_list) == 0:
            log.error("Can't find groups to add to TC. Check ldap groups and/or ldap custom groups is set")
            raise Exception('Config error in "ldap" section')


    #Init mutators
    service = services()

    #Reformat ldap group list
    formatted_ldap_group_list = service.reformat_ldap_group_list(ldap_group_list)

    #Remove deleted group from list to prevent AD sync lag
    with localCache(config) as local_cache:
        corrected_ldap_group_list = local_cache.correct_ldap_group_list(formatted_ldap_group_list)

    # Connect to TeamCity
    tc = TeamCityClient(config,config)

    #Get teamcity groups
    teamcity_group_list = tc.get_tc_groups()

    #Get groups to create it in teamcity
    teamcity_new_groups = service.diff_ldap_teamcity_groups(corrected_ldap_group_list, teamcity_group_list)

    #Create new groups in Teamcity #1
    if len(teamcity_new_groups) > 0:
        tc.create_groups(teamcity_new_groups)

    #Add Teamcity keys to corrected ldap group list
    renewed_teamcity_group_list = tc.get_tc_groups()
    ldap_group_list_with_keys = service.sim_ldap_teamcity_groups(corrected_ldap_group_list,
                                                                 renewed_teamcity_group_list,
                                                                 teamcity_new_groups)

    # Connect to XML? =)
    xml = xml_changer(config)

    # Get XML groups
    xml_group_list = xml.get_current_groups()
    log.debug("XML group count is {}".format(len(xml_group_list)))

    #Get list of groups must be deleted from Teamcity
    tc_deprecated_groups = service.diff_xml_ldap_groups(xml_group_list, ldap_group_list_with_keys)

    # Delete groups remove from AD from Teamcity #1
    if len(tc_deprecated_groups) > 0:
        tc.delete_groups(tc_deprecated_groups)

    #Create new groups in xml
    xml.reganerate_ldap_xml(ldap_group_list_with_keys)

def quit(signo, _frame):
    print("Interrupted by %d, shutting down" % signo)
    exit.set()

if __name__ == '__main__':
    log = initLog()
    log.info("Startup Teamcity Active Directory Synchronizer")

    exit = Event()
    while not exit.is_set():
        try:
            import signal
            for sig in ('TERM', 'HUP', 'INT'):
                signal.signal(getattr(signal, 'SIG'+sig), quit)
            main()
        except Exception as e:
            log.error(e)
        #wait next loop hoop
        exit.wait(1)

    log.info("Successful stop Teamcity Active Directory Synchronizer")
