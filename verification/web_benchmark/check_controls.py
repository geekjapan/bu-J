import json
from pathlib import Path
from uuid import uuid4

from decision_browser.web.agent import WebAgent
from decision_browser.web.browser import Browser
from decision_browser.web.environment import ManagedChrome
from decision_browser.web.tasks import get_task, verify_task


def main():
    output=Path('.audit/web-controls')/uuid4().hex
    output.mkdir(parents=True)
    receipts=[]
    print(output,flush=True)
    with ManagedChrome(output/'chrome') as chrome:
        task=get_task('hotel')
        browser=Browser(task,chrome.manifest['endpoint'])
        try:
            page=browser.observe()
            for kind,label,value in [('fill','Destination','Lisbon'),('click','Find stays',None),
                    ('select','Stay category → Design',None),('click','Free cancellation',None),('click','View Casa Flora',None)]:
                matches=[a for a in page['actions'] if a['kind']==kind and a['label']==label]
                assert len(matches)==1,(kind,label,[(a['kind'],a['label']) for a in page['actions']])
                action=matches[0]
                browser.act(action,page,text=value)
                after=browser.observe()
                verified=browser.fresh(after) and WebAgent.effect_verified(action,page,after,value)
                receipts.append({'action':action,'effect_verified':verified})
                (output/'actions.json').write_text(json.dumps(receipts,indent=2,ensure_ascii=False)+'\n', encoding='utf-8')
                assert verified,label
                page=after
            final=verify_task(task,browser.observe())
            (output/'final.json').write_text(json.dumps({'verification':final,'page':page},indent=2,ensure_ascii=False)+'\n', encoding='utf-8')
            (output/'final.png').write_bytes(browser.screenshot())
            assert final['passed']
            print(json.dumps({'verified':True,'actions':len(receipts)}),flush=True)
        finally:browser.close()


if __name__=='__main__':main()
