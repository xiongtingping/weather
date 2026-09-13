from common import *
import subprocess,tarfile
target=OUT/'baseline_b86dc71c'
target.mkdir(exist_ok=True)
proc=subprocess.Popen(['git','archive','b86dc71c7634344fe987de6d9ccd0f3751da5ef8'],cwd=ROOT,stdout=subprocess.PIPE)
size=0
with tarfile.open(fileobj=proc.stdout,mode='r|') as archive:
    for member in archive:
        size+=member.size
        if size>160*1024**2 or member.size>48*1024**2:raise RuntimeError('ARCHIVE_CAP')
        if member.issym() or member.islnk():continue
        archive.extract(member,target,filter='data')
assert proc.wait()==0
print('BASELINE_EXTRACTED',size,str(target))
