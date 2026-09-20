"""Retire only owned legacy bridge networks whose fixed subnets would collide.

Container images, volumes, IPs, aliases and rollback connectivity are preserved.
No firewall, host networking, external network or other project's network changes.
"""
from __future__ import annotations
import ipaddress


def preflight(config,containers,docker,legacy_project):
    selected=[];owned={c['Id']:c for c in containers}
    if not owned:return selected
    requested=[]
    for key,network in config.get('networks',{}).items():
        for item in (network.get('ipam') or {}).get('config') or []:
            if item.get('subnet'):requested.append((key,network,ipaddress.ip_network(item['subnet'],strict=False)))
    if not requested:return selected
    ids=docker.run(['network','ls','--format','{{.ID}}']).stdout.split()
    if not ids:return selected
    inspected=docker.json(['network','inspect',*ids])
    for current in inspected:
        overlaps=[]
        for item in (current.get('IPAM') or {}).get('Config') or []:
            if not item.get('Subnet'):continue
            subnet=ipaddress.ip_network(item['Subnet'],strict=False)
            overlaps.extend((k,n) for k,n,desired in requested if subnet.version==desired.version and subnet.overlaps(desired))
        if not overlaps:continue
        labels=current.get('Labels') or {}
        if labels.get('com.docker.compose.project')=='codepier':continue
        if labels.get('com.docker.compose.project')!=legacy_project:raise RuntimeError('Configured proxy subnet overlaps an unrelated network; nothing was stopped')
        if any(n.get('external') for _,n in overlaps):raise RuntimeError('An external network cannot be renamed automatically')
        if (current.get('Driver')!='bridge' or current.get('Scope')!='local' or current.get('Ingress')
                or current.get('IPAM',{}).get('Driver','default')!='default'):
            raise RuntimeError('Legacy network uses a custom driver; migration was not started')
        members=docker.run(['ps','-aq','--filter','network='+current['Id']]).stdout.split()
        if any(not any(key.startswith(member) for key in owned) for member in members):raise RuntimeError('Legacy network has other containers; migration was not started')
        endpoints=[]
        for id,container in owned.items():
            attachment=container.get('NetworkSettings',{}).get('Networks',{}).get(current['Name'])
            if not attachment:continue
            if attachment.get('Links') or attachment.get('DriverOpts') or (attachment.get('IPAMConfig') or {}).get('LinkLocalIPs'):
                raise RuntimeError('Custom endpoint options need an explicit network migration')
            ipam=attachment.get('IPAMConfig') or {}
            endpoints.append({'container':id,'ipv4':ipam.get('IPv4Address') or attachment.get('IPAddress'),
                              'ipv6':ipam.get('IPv6Address') or attachment.get('GlobalIPv6Address'),
                              'aliases':attachment.get('Aliases') or [],'disconnect_requested':False})
        selected.append({'name':current['Name'],'id':current['Id'],'driver':'bridge','labels':labels,
                         'ipam':current.get('IPAM',{}),'options':current.get('Options') or {},
                         'internal':bool(current.get('Internal')),'ipv6':bool(current.get('EnableIPv6')),
                         'attachable':bool(current.get('Attachable')),'endpoints':endpoints,'remove_requested':False})
    return selected


def retire(records,docker,save):
    for record in records:
        current=docker.json(['network','inspect',record['id']])[0]
        if current['Name']!=record['name'] or (current.get('Labels') or {})!=record['labels']:raise RuntimeError('Legacy network identity changed')
        for endpoint in record['endpoints']:
            endpoint['disconnect_requested']=True;save()
            docker.run(['network','disconnect','--force',record['id'],endpoint['container']])
        record['remove_requested']=True;save()
        docker.run(['network','rm',record['id']])


def restore(records,docker):
    for record in records:
        if not any(e.get('disconnect_requested') for e in record['endpoints']) and not record.get('remove_requested'):continue
        names=docker.run(['network','ls','--format','{{.Name}}']).stdout.splitlines()
        if record['name'] in names:
            current=docker.json(['network','inspect',record['name']])[0]
            if (current.get('Labels') or {})!=record['labels'] or current.get('IPAM',{}).get('Config')!=record['ipam'].get('Config'):
                raise RuntimeError('Original network name is occupied; no foreign network was changed')
        else:
            args=['network','create','--driver','bridge']
            if record['internal']:args.append('--internal')
            if record['ipv6']:args.append('--ipv6')
            if record['attachable']:args.append('--attachable')
            for key,value in record['labels'].items():args+=['--label',key+'='+value]
            for key,value in record['options'].items():args+=['--opt',key+'='+value]
            for item in record['ipam'].get('Config',[]):
                for field,flag in [('Subnet','--subnet'),('Gateway','--gateway'),('IPRange','--ip-range')]:
                    if item.get(field):args+=[flag,item[field]]
                for key,value in (item.get('AuxiliaryAddresses') or {}).items():args+=['--aux-address',key+'='+value]
            args.append(record['name']);docker.run(args)
        for endpoint in record['endpoints']:
            if not endpoint['disconnect_requested']:continue
            container=docker.json(['inspect',endpoint['container']])[0]
            if record['name'] in container.get('NetworkSettings',{}).get('Networks',{}):continue
            args=['network','connect']
            if endpoint['ipv4']:args+=['--ip',endpoint['ipv4']]
            if endpoint['ipv6']:args+=['--ip6',endpoint['ipv6']]
            for alias in endpoint['aliases']:args+=['--alias',alias]
            args += [record['name'],endpoint['container']];docker.run(args)
