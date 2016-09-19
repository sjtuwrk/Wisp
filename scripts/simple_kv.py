import rdma
import rdma.ibverbs as ibv
import rdma.IBA as IBA
import rdma.vtools
from rdma.tools import clock_monotonic
from rdma.vtools import BufferPool
import sys
import socket
import contextlib
from mmap import mmap
import pickle
import time
import copy
import random
import string
from collections import namedtuple
from collections import deque

display_mode = False

ip_port = 4444
tx_depth = 100
memsize = 1024*100
buffersize = 256
buffernum = 8
keysize = 10
payloadsize = 100

infotype = namedtuple('infotype', 'path addr rkey size iters')

def id_generator(size=6, chars=string.ascii_uppercase + string.digits):
    return ''.join(random.choice(chars) for _ in range(size))

def print_info(*messages):
    if display_mode:
        for message in enumerate(messages):
            print message
        
class BufferPoolEnhanced(BufferPool):
    def recharge(self):
        self._buffers = deque(xrange(self.count),self.count);
        return self.reset_count()
    
    def reset_count(self):
        return self.count-1;
    
    def count_minus(self,count):
        res = count - 1
        if(res<0):
            res = self.recharge()
        return res
    
class Endpoint(object):
    ctx = None;
    pd = None;
    cq = None;
    mr = None;
    peerinfo = None;

    def __init__(self,sz,dev):
        self.ctx = rdma.get_verbs(dev)
        self.cc = self.ctx.comp_channel();
        self.cq = self.ctx.cq(2*tx_depth,self.cc)
        self.poller = rdma.vtools.CQPoller(self.cq);
        self.pd = self.ctx.pd()
        self.qp = self.pd.qp(ibv.IBV_QPT_RC,tx_depth,self.cq,tx_depth,self.cq);
        self.mem = mmap(-1, sz)
        self.mr = self.pd.mr(self.mem,
                             ibv.IBV_ACCESS_LOCAL_WRITE|ibv.IBV_ACCESS_REMOTE_WRITE)
        self.pool = BufferPoolEnhanced(self.pd,buffernum,buffersize*buffernum)

    def __enter__(self):
        return self;

    def __exit__(self,*exc_info):
        return self.close();

    def close(self):
        if self.ctx is not None:
            self.ctx.close();

    def connect(self, peerinfo):
        self.peerinfo = peerinfo
        self.qp.establish(self.path,ibv.IBV_ACCESS_REMOTE_WRITE);

    def write(self):
        swr = ibv.send_wr(wr_id=0,
                          remote_addr=self.peerinfo.addr,
                          rkey=self.peerinfo.rkey,
                          sg_list=self.mr.sge(),
                          opcode=ibv.IBV_WR_RDMA_WRITE,
                          send_flags=ibv.IBV_SEND_SIGNALED)

        n = 1
        depth = min(tx_depth, n, self.qp.max_send_wr)

        tpost = clock_monotonic()
        for i in xrange(depth):
            self.qp.post_send(swr)

        completions = 0
        posts = depth
        for wc in self.poller.iterwc(timeout=3):
            if wc.status != ibv.IBV_WC_SUCCESS:
                raise ibv.WCError(wc,self.cq,obj=self.qp);
            completions += 1
            if posts < n:
                self.qp.post_send(swr)
                posts += 1
                self.poller.wakeat = rdma.tools.clock_monotonic() + 1;
            if completions == n:
                break;
        else:
            raise rdma.RDMAError("CQ timed out");

        tcomp = clock_monotonic()
    def send(self,buf):
        buf_idx = self.pool.pop();
        self.pool.copy_to(buf,buf_idx);
        self.qp.post_send(self.pool.make_send_wr(buf_idx,len(buf),self.path));
    
    def recv(self):
        self.pool.post_recvs(self.qp,1)
        
def run_server(dev):
    database = {}

    def execute(command):
        command = command.split(' ', 1)
        if command[0] in 'GET get Get':
            res = database.get(command[1])
            print_info(command[1]+' :', res)
            return str(res) 
        elif command[0] in 'PUT put Put SET set Set':
            command = command[1].split(' ', 1)
            if len(command)==1:
                print_info('No value set')
                return 'FAIL'
            else:
                database[command[0]] = command[1]
                print_info(command[0]+' : '+command[1])
                return 'SUCCESS'
        elif command[0] in 'DEL del Del DELETE delete Delete':
            database.pop(command[1], None)
            print_info(command[1]+' : None')
            return 'SUCCESS'
            
        else:
            print_info( 'Wrong command')
            return 'FAIL'

        #print database

    print 'server running..'
    ret = socket.getaddrinfo(None,str(ip_port),0,
                             socket.SOCK_STREAM,0,
                             socket.AI_PASSIVE);
    ret = ret[0];
    with contextlib.closing(socket.socket(ret[0],ret[1])) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(ret[4]);
        sock.listen(1)

        print "Listening port..."
        s,addr = sock.accept()
        with contextlib.closing(s):
            buf = s.recv(1024)
            peerinfo = pickle.loads(buf)

            with Endpoint(peerinfo.size, dev) as end:
                back_path = copy.deepcopy(peerinfo.path)
                back_path.reverse()
                with rdma.get_gmp_mad(end.ctx.end_port,verbs=end.ctx) as umad:
                    end.path = peerinfo.path;
                    end.path.end_port = end.ctx.end_port;
                    rdma.path.fill_path(end.qp,end.path);
                    rdma.path.resolve_path(umad,end.path);

                    s.send(pickle.dumps(infotype(path=end.path,
                                                 addr=end.mr.addr,
                                                 rkey=end.mr.rkey,
                                                 size=None,
                                                 iters=None)))

                    print "path to peer %r\nMR peer raddr=%x peer rkey=%x"%(
                        end.path,peerinfo.addr,peerinfo.rkey);

                    end.connect(peerinfo)

                    # Synchronize the transition to RTS
                    s.send("ready");
                    s.recv(1024);
                        
                    buffer_count = end.pool.reset_count()
                    while True:
                        #s.recv(1024);
                        while True:
                            cond = (end.mem.read(1)!='\x00')
                            end.mem.seek(-1,1)
                            if cond:
                                break
                        
                        raw_str = end.mem.readline()
                        request = raw_str.split('\n', 1)[0]

                        print_info('Received: ' + request)
                        response = execute(request)
                        #umad.sendto(str_response,peerinfo.path.reverse(for_reply=True))
                        end.send(response+'\n')

                        buffer_count = end.pool.count_minus(buffer_count) 

                    s.shutdown(socket.SHUT_WR);
                    s.recv(1024);

                
def run_client(hostname,dev):
    print 'client running..'
    with Endpoint(memsize, dev) as end:
        with rdma.get_gmp_mad(end.ctx.end_port,verbs=end.ctx) as umad:
            ret = socket.getaddrinfo(hostname,str(ip_port),0,
                                     socket.SOCK_STREAM);
            ret = ret[0];
            with contextlib.closing(socket.socket(ret[0],ret[1])) as sock:
                sock.connect(ret[4]);

                path = rdma.path.IBPath(dev,SGID=end.ctx.end_port.default_gid);
                rdma.path.fill_path(end.qp,path,max_rd_atomic=0);
                path.reverse(for_reply=True);

                sock.send(pickle.dumps(infotype(path=path,
                                                addr=end.mr.addr,
                                                rkey=end.mr.rkey,
                                                size=memsize,
                                                iters=1)))
                buf = sock.recv(1024)
                peerinfo = pickle.loads(buf)

                end.path = peerinfo.path;
                end.path.reverse(for_reply=True);
                end.path.end_port = end.ctx.end_port;

                print "path to peer %r\nMR peer raddr=%x peer rkey=%x"%(
                    end.path,peerinfo.addr,peerinfo.rkey);

                end.connect(peerinfo)
                
                # Synchronize the transition to RTS
                sock.send("Ready");
                sock.recv(1024);

                start_time = time.time()
                buffer_count = end.pool.reset_count()
                response = [0]
                request_count = 0
                while True:
                    #write request
                    #request = raw_input()
                    op = random.randint(0,1)
                    if op==0:
                        request = 'get '+id_generator(3,'0123456789')
                    elif op==1:
                        request = 'put '+id_generator(3,'0123456789')+' '+id_generator(128,'0123456789')

                    end.pool.copy_to('\0',buffer_count)

                    end.recv()
                    end.mem.write(request+'\n')
                    end.write()
                
                    request_count +=1
                    if request_count>200:
                        break
                    
                    #sock.send("Sent");

                    print_info('Sent: ' + request)
                    
                    #poll response
                    while True:
                        response = end.pool.copy_from(buffer_count)
                        if response[0]!=0:
                            break
                        
                    print_info(response.split('\n', 1)[0])
                    #for i in xrange(end.pool.count):
                    #    print i,end.pool.copy_from(i).split('\n', 1)[0]
                    
                    buffer_count = end.pool.count_minus(buffer_count) 
                    
                print("--- %s seconds, %s requests ---" % (time.time() - start_time,request_count))
                
                sock.shutdown(socket.SHUT_WR);
                sock.recv(1024);


                print "---client end"
            print "---sock close"
    print "--- endpoint close"

def main():
    usage_str = '''
    Usage: python ./simple_kv.py server
           python ./simple_kv.py client hostname
    '''
    
    if len(sys.argv) ==1:
        print usage_str
        return


    if sys.argv[1]=='server':
        run_server(rdma.get_end_port())
    elif sys.argv[1]=='client':
        run_client(sys.argv[2], rdma.get_end_port())
    else:
        print usage_str
        return
    


if __name__ == "__main__":
    main()
