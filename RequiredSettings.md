## we are using EPic , I have registered the app on epic and all scope has been assigned , i am using this app for some other backend app 
## I beleive we can use same app for other backend app too, I only changed keyID to V2
## I am using zrok for tunneling 

    ClientId": "dfc59c89-fcc7-47ff-a453-7af76f63ee77",
    "TokenEndpoint": "https://fhir.epic.com/interconnect-fhir-oauth/oauth2/token",
    "FhirBaseUrl": "https://fhir.epic.com/interconnect-fhir-oauth/api/FHIR/R4",
    "PrivateKeyPath": "../privatekey.pem",
    "KeyId": "my-epic-key-v2",
    "GroupId": "e3iabhmS8rsueyz7vaimuiaSmfGvi.QwjVXJANlPOgR83"
    
    "OpenAI": {
    "ApiKey": "REDACTED — store in .env as OPENAI_API_KEY",
    "Model": "gpt-4o-mini"

## Zrok url registered in epic app is 

    https://pcmsmartbackendapp1.share.zrok.io 

## private key and public key is already there in workspace , same is used for other backend app, I hope it will not create any problem

