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
    TeamCity sync configuration parse class
    Provides methods for parsing and retrieving config entries
    """

    def __init__(self, parser):
        try:
                self.ldap_uri = parser.get('ldap', 'uri')
                self.ldap_user = parser.get('ldap', 'binduser')
                self.ldap_pass = parser.get('ldap', 'bindpass')
                self.groups_search_scope = parser.get('ldap', 'groups_search_scope')
                self.groups_search_base_list = re.findall(r"['\"](.*?)['\"]", parser.get('ldap', 'groups_search_base_list'))
                self.custom_groups_list = re.findall(r"['\"](.*?)['\"]", parser.get('ldap', 'custom_groups_list'))

                self.tc_server = parser.get('teamcity', 'server')
                self.tc_username = parser.get('teamcity', 'username')
                self.tc_password = parser.get('teamcity', 'password')
                self.tc_verify_certificate = parser.get('teamcity', 'verify_certificate')

                self.path_ldap_mapping = parser.get('xml', 'path_ldap_mapping')

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

        groups_list_dn = list()
        for dn in search_base:
            self.conn.search(search_base=dn,
                             search_filter='(objectclass=group)',
                             attributes=["distinguishedName"],
                             search_scope=search_scope)

            for e in self.conn.entries:
                groups_list_dn.append(e.distinguishedName[0])

        if not groups_list_dn:
            return(None)

        return groups_list_dn


class TeamCityClient(object):
    def __init__(self, config):
        self.rest_url = '{url}/app/rest/'.format(url=config.tc_server)
        self.session = requests.Session()
        self.session.auth = (config.tc_username, config.tc_password)
        self.session.headers.update({'Content-type': 'application/json', 'Accept': 'application/json', 'Origin': config.tc_server})
        self.session.verify = config.tc_verify_certificate

    def get_tc_groups(self):
        url = self.rest_url + 'userGroups'
        groups_in_tc = self.session.get(url, verify=self.session.verify).json()
        return [group for group in groups_in_tc['group']]

    def create_groups(self, groups_list):
        created_groups_prop = list()
        for group_obj in groups_list:
            #print("Creating group {} in TC".format(group_obj.get("name")))
            url = self.rest_url + 'userGroups'
            key = ''.join(random.choice("{}{}".format(ascii_letters, digits)) for i in range(16))
            created_groups_prop.append({"teamcityGroupKey": key, "ldapGroupDn": group_obj.get("dn")})

            data = json.dumps({"name": group_obj.get("name"), "key": key})
            resp = self.session.post(url, verify=self.session.verify, data=data)
            if resp.status_code == 200:
                self.tc_groups = TeamCityClient.get_tc_groups(self)
                created_groups_prop.append({"dn": group_obj.get("dn"), "key": key})
            else:
                print("Error: Couldn't create group {}\n{}".format(group_obj.get("name"), resp.content))
        return created_groups_prop

    def delete_groups(self, groups_list):
        for group_obj in groups_list:
            url = '{url}userGroups/key:{key}'.format(url=self.rest_url, key=group_obj.get('key'))
            print(url)
            resp = self.session.delete(url, verify=False)
            print("resp code is" + str(resp.status_code))
            if resp.status_code == 204:
                return True
            else:
                print("Error: Couldn't delete group {}\n{}".format(group_obj.get("ldapGroupDn"), resp.content))
                return False


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
            root.append( etree.Element("group-mapping", attrib={'teamcityGroupKey': group.get('key'), 'ldapGroupDn': group.get('ldapGroupDn')}))
        my_tree = etree.ElementTree(root)
        my_tree.write(self.path_ldap_mapping, pretty_print=True, doctype='<!DOCTYPE mapping SYSTEM "ldap-mapping.dtd">')

    def backup_current_config(self):
        print("hope and pray to god")

class Comparators(object):
    """
    Some Comparators.
    Provides methods for diff and similarity entries
    """
    def sim_ldap_teamcity_groups(self, ldap_group_list, teamcity_group_list):
        sim_groups = list()
        for group in teamcity_group_list:
            for ldap_group in ldap_group_list:
                ldap_group_name = re.findall(r"CN\=(.*?),", ldap_group)[0]
                if ldap_group_name in group['name']:
                    ldap_group_prop = {"name": ldap_group_name, "ldapGroupDn": ldap_group, "key": group.get('key')}
                    sim_groups.append(ldap_group_prop)
        return sim_groups

    def diff_ldap_teamcity_groups(self, ldap_group_list, teamcity_group_list):
        teamcity_group_name_list = [group['name'] for group in teamcity_group_list]
        diff_groups = list()
        for ldap_group in ldap_group_list:
            ldap_group_name = re.findall(r"CN\=(.*?),", ldap_group)[0]
            if ldap_group_name not in teamcity_group_name_list:
                ldap_group_prop = {"name": ldap_group_name, "dn": ldap_group}
                diff_groups.append(ldap_group_prop)
        return diff_groups

    def diff_xml_ldap_groups(self, xml_group_list, ldap_new_groups):
        ldap_group_name_list = [group.get("ldapGroupDn") for group in ldap_new_groups]
        diff_groups = list()
        for xml_group_obj in xml_group_list:
            if xml_group_obj.get('ldapGroupDn') not in ldap_group_name_list:
                diff_groups.append({'ldapGroupDn': xml_group_obj.get('ldapGroupDn'), "key": xml_group_obj.get('teamcityGroupKey')})
        return diff_groups

exit = Event()

def main():
    while not exit.is_set():
        #Start application
        log = initLog()
        log.info("Start Teamcity Active Directory Synchronizer")

        # Parse CLI arguments
        args = get_args()

        # Read config file
        parser = configparser.RawConfigParser()
        parser.read(args.file)

        # Create config object from config file
        config = createConfig(parser)

        # Connect to LDAP
        with LDAPConnector(args, config) as ldap_conn:
            ldap_group_list = ldap_conn.search_groups(config.groups_search_base_list,config.groups_search_scope)

            #Add custom groups from config
            if config.custom_groups_list:
                ldap_group_list = ldap_group_list + config.custom_groups_list

        # Connect to TeamCity
        tc = TeamCityClient(config)
        teamcity_group_list = tc.get_tc_groups()

        # Connect to XML? =)
        xml = xml_changer(config)
        xml_group_list = xml.get_current_groups()

        #Get all differents and similarities
        compare = Comparators()

        #Get groups to create it in teamcity
        teamcity_new_groups = compare.diff_ldap_teamcity_groups(ldap_group_list, teamcity_group_list)

        #Create new groups in Teamcity
        tc.create_groups(teamcity_new_groups)

        #Get new ldap groups to generate ldap-mapping.xml
        ldap_new_groups = compare.sim_ldap_teamcity_groups(ldap_group_list, teamcity_group_list)

        #Get list of groups must be deleted from Teamcity
        tc_deprecated_groups = compare.diff_xml_ldap_groups(xml_group_list, ldap_new_groups)

        #Delete new groups in Teamcity
        tc.delete_groups(tc_deprecated_groups)

        #Create new groups in xml
        xml.reganerate_ldap_xml(ldap_new_groups)

        #wait next loop hoop
        exit.wait(5)
    print("All done!")

def quit(signo, _frame):
    print("Interrupted by %d, shutting down" % signo)
    exit.set()

if __name__ == '__main__':

    import signal
    for sig in ('TERM', 'HUP', 'INT'):
        signal.signal(getattr(signal, 'SIG'+sig), quit)

    main()