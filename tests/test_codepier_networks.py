"""Fixed proxy subnet ownership and exact rollback command contracts."""
import copy
from types import SimpleNamespace
import pytest
from scripts import migrate_hub_networks as n


def fixture():
    old={'Id':'a'*64,'NetworkSettings':{'Networks':{'legacy_proxy':{'IPAddress':'172.30.87.3','IPAMConfig':None,'Aliases':['hub']}}}}
    net={'Id':'b'*64,'Name':'legacy_proxy','Driver':'bridge','Scope':'local','Labels':{'com.docker.compose.project':'legacy'},
         'IPAM':{'Driver':'default','Config':[{'Subnet':'172.30.87.0/24','Gateway':'172.30.87.1'}]},'Options':{}}
    class Docker:
        def __init__(self):self.commands=[];self.current=copy.deepcopy(net);self.foreign=False;self.exists=True;self.connected=True
        def run(self,cmd,**kw):
            self.commands.append(cmd);out=''
            if cmd[:3]==['network','ls','--format']:out=('legacy_proxy' if '{{.Name}}' in cmd else 'b'*12) if self.exists else ''
            if cmd[:2]==['ps','-aq']:out='f'*12 if self.foreign else 'a'*12
            if cmd[:2]==['network','rm']:self.exists=False
            if cmd[:2]==['network','create']:self.exists=True
            if cmd[:2]==['network','disconnect']:self.connected=False
            if cmd[:2]==['network','connect']:self.connected=True
            return SimpleNamespace(stdout=out,returncode=0)
        def json(self,cmd):
            if cmd[0]=='inspect':return [{'NetworkSettings':{'Networks':{'legacy_proxy':{}} if self.connected else {}}}]
            return [self.current]
    return {'networks':{'proxy':{'ipam':{'config':[{'subnet':'172.30.87.0/24'}]}}}},old,Docker()


def test_fixed_subnet_releases_owned_network_and_restores_original_ip():
    config,container,docker=fixture();plans=n.preflight(config,[container],docker,'legacy');assert len(plans)==1
    saved=[];n.retire(plans,docker,lambda:saved.append(copy.deepcopy(plans)))
    assert not docker.exists and len(saved)==2
    n.restore(plans,docker)
    assert docker.exists and docker.connected
    assert ['network','connect','--ip','172.30.87.3','--alias','hub','legacy_proxy','a'*64] in docker.commands


@pytest.mark.parametrize('problem',['foreign-container','unrelated-network','external','custom-driver'])
def test_ambiguous_networks_never_reach_mutation(problem):
    config,container,docker=fixture()
    if problem=='foreign-container':docker.foreign=True
    elif problem=='unrelated-network':docker.current['Labels']['com.docker.compose.project']='other'
    elif problem=='external':config['networks']['proxy']['external']=True
    else:docker.current['Driver']='overlay'
    with pytest.raises(RuntimeError):n.preflight(config,[container],docker,'legacy')
    assert not any(cmd[:2] in [['network','rm'],['network','disconnect']] for cmd in docker.commands)


def test_docker_builtin_networks_with_null_ipam_are_ignored():
    config,container,docker=fixture();docker.current['IPAM']['Config']=None
    assert n.preflight(config,[container],docker,'legacy')==[]
