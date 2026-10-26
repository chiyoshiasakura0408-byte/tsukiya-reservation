"""Run repository unit tests with external HTTP/socket traffic prohibited."""
import os, socket, sys, unittest, urllib.request
from urllib.parse import urlparse
# Drop inherited service credentials and HTTP proxies before importing the app.
for key in list(os.environ):
    if any(s in key.upper() for s in ('TOKEN','SECRET','PASSWORD','SMTP','TWILIO','API_KEY','PROXY')):
        os.environ.pop(key,None)
os.environ['NO_PROXY']='127.0.0.1,localhost,::1'
original_connect=socket.socket.connect
original_connect_ex=socket.socket.connect_ex
original_urlopen=urllib.request.urlopen

def allowed(address):
    return isinstance(address,tuple) and str(address[0]) in ('127.0.0.1','::1','localhost')
def connect(self,address):
    if not allowed(address):raise AssertionError('Offline test guard: external socket disabled')
    return original_connect(self,address)
def connect_ex(self,address):
    if not allowed(address):raise AssertionError('Offline test guard: external socket disabled')
    return original_connect_ex(self,address)
def urlopen(req,*args,**kwargs):
    url=req.full_url if hasattr(req,'full_url') else req
    if urlparse(url).hostname not in ('127.0.0.1','localhost','::1'):
        raise AssertionError('Offline test guard: external HTTP disabled')
    return original_urlopen(req,*args,**kwargs)
socket.socket.connect=connect
socket.socket.connect_ex=connect_ex
urllib.request.urlopen=urlopen
sys.path.insert(0,os.getcwd())
patterns=sys.argv[1:] or ['test_*.py']
suite=unittest.TestSuite(unittest.defaultTestLoader.discover('tests',pattern=p) for p in patterns)
result=unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(not result.wasSuccessful())
