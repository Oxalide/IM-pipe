# IM-pipe

Forward IM from and to **Slack**, **Hipchat** and **Flowdock**.

## Features

IM-pipe is built on two type of threads: **Reader** and **Writer**. Both support some IM systems :

### Reader

* Hipchat *(supports message pattern matching in order to forward specific message to a writer)*
* Slack
* Flowdock

### Writer

* Hipchat
* Slack
* Flowdock

### Adding an IM-pipe instance for a client

IM-pipe runs as a daemon. To create a new instance for one client, you have just to follow this procedure : 

  * Create a new YAML configuration file and add your own configuration properties *(see example in "Configuration" part)*
  
### Configuration

Here, a sample configuration with two **Readers** : *Hipchat* and *Slack* and, two **Writers** : *Flowdock* and *Hipchat*

```
- read: backend=hipchat api_token=<HIPCHAT_API_TOKEN> server==<HIPCHAT_SERVER>
  channels:
    - name: <HIPCHAT_ROOM>
      forward_to: "<FLOWDOCK_ORG>|<FLOWDOCK_FLOW>@<WRITER_NAME_1>"
      include_pattern:
        - "^@toto"
- write: backend=flowdock alias=<WRITER_NAME_1> api_token=<FLOWDOCK_API_TOKEN>
- read: backend=slack api_token=<SLACK_API_TOKEN>
  channels:
    - name: <SLACK_ROOM>
      forward_to: "<HIPCHAT_ROOM>@<WRITER_NAME_2>"
- write: backend=hipchat alias=<WRITER_NAME_2> server=<HIPCHAT_SERVER> api_token=<TOKEN>
```

### Manual installation

To use this application, please follow the steps below : 

  * git clone https://github.com/Oxalide/IM-pipe.git
  * cd IM-pipe
  * virtualenv env
  * source env/bin/activate
  * pip install -r requirements.txt

When installation is done, you need to add a configuration file and launch the program :

  * python bot.py *(if your configuration file name is **config.yml**)*
  * python bot.py --config 'PATH CONFIG FILE' *(if your configuration file name is different to **config.yml**)*
