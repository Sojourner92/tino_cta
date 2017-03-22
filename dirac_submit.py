from DIRAC.Core.Base import Script
Script.parseCommandLine()
from DIRAC.Interfaces.API.Job import Job
from DIRAC.Interfaces.API.Dirac import Dirac

import glob

dirac = Dirac()

j = Job()
j.setCPUTime(500)

file1='/vo.cta.in2p3.fr/user/c/ciro.bigongiari/MiniArray9/Simtel/gamma/run1011.simtel.gz'
file2='/vo.cta.in2p3.fr/user/c/ciro.bigongiari/MiniArray9/Simtel/proton/run10011.simtel.gz'
j.setInputData([file1])

pilot_args = 'fit_events_hillas.py -i . -f {} -m 50 --tail --events_dir ./ --store'.format(file2.split('/')[-1])

input_sandbox = glob.glob('modules/*py')+['helper_functions.py',
                                          'dirac_pilot.sh',
                                           pilot_args.split()[0]]
print "running as: ", pilot_args
print "with input_sandbox:", input_sandbox

j.setExecutable('dirac_pilot.sh', pilot_args)
j.setInputSandbox( input_sandbox )
j.setName('hillas fit test')

print "submitting job"
res = dirac.submit(j)
print 'Submission Result: ', res['Value']