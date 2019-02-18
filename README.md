# teamcity-AD_ldap-sync
Teamcity side python application. For sync groups from Active directory special organization units(OU) to Teamcity.

### Project structure
* requirements.txt - Python dependencies.
* synchronizer.conf - Application config file.
* ad_group_synchronizer.py - Application.

### Configuration structure synchronizer.conf
```
[ldap]
uri - Active directory(AD) domain controller address
binduser - Username of AD integration user.
bindpass - Password of AD integration user.
groups_search_scope - search scope, default "LEVEL" it mean search only in OU, instead of "SUBTREE" search in OU recursive. SUBTREE IS EVIL
groups_search_base_list - list of organization units(OU) with groups you wanna add to Teamcity

[teamcity]
server - Teamcity api address
username - Username of teamcity integration user.
password - Password of teamcity integration user.
verify_certificate - SSL yes/no

[xml]
path_ldap_mapping - There should be path of ldap-mapping.xml file with one or more group mappings defined.
```

### An example of running
```
python3 ad_group_synchronizer.py -f synchronizer.conf
```
