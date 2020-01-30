Vue.component('tweet', {
    data: function () {
        return {
            excludeFromDeletion: false
        }
    },
    props: ["tweet"],
    created: function () {
        this.excludeFromDeletion = this.tweet.exclude;
    },
    mounted: function () {
        var that = this;
        this.$nextTick(function () {
            // TODO: For some reason this isn't rendering...
            twttr.widgets.createTweet(
                that.tweet.status_id,
                document.getElementById(that.tweetId),
                { 'dnt': true }
            );
        })
    },
    computed: {
        tweetId: function () {
            return "tweet-" + this.tweet.status_id
        }
    },
    template: `
        <div>
            <div class="info">
                <label>
                    <input type="checkbox" v-bind:checked="excludeFromDeletion" />
                    <span v-if="excludeFromDeletion">Excluded from deletion</span>
                    <span v-else>Staged for deletion</span>
                </label>
                <div class="stats">{{ tweet.retweet_count }} retweets, {{ tweet.favorite_count }} likes</div>
            </div>
            <div v-bind:id="tweetId"></div>
        </div>
    `
})

Vue.component('tweets', {
    data: function () {
        return {
            loading: false,
            tweets: [],
            filteredIndices: [], // Indices for tweets after applying filter
            pageIndices: [], // Indices of tweets on the current page
            filterQuery: "",
            showReplies: true,
            page: 0,
            numPages: 1,
            countPerPage: 50,
            info: ""
        }
    },
    created: function () {
        this.fetchTweets()
    },
    methods: {
        fetchTweets: function () {
            var that = this;
            this.loading = true;

            // Get all saved tweets
            fetch("/api/tweets")
                .then(function (response) {
                    if (response.status !== 200) {
                        console.log('Error fetching tweets, status code: ' + response.status);
                        return;
                    }
                    response.json().then(function (data) {
                        that.tweets = data['tweets'];
                        that.filterTweets();
                        that.loading = false;
                    })
                })
                .catch(function (err) {
                    console.log("Error fetching tweets", err)
                    that.loading = false;
                })
        },
        filterTweets: function (page = 0) {
            this.page = page;

            // filteredIndices is a list of tweets array indices that match the filter settings
            for (var i in this.tweets) {
                if (this.tweets[i]["text"].toLowerCase().includes(this.filterQuery.toLowerCase())) {
                    if (this.showReplies || (!this.showReplies && !this.tweets[i]["is_reply"])) {
                        this.filteredIndices.push(i);
                    }
                }
            }

            this.numPages = Math.ceil(this.filteredIndices.length / this.countPerPage);
            if (this.page >= this.numPages) {
                this.page = 0;
            }

            // pageIndices is a list of tweets array indices to get displayed on the current page
            this.pageIndices = [];
            for (var i = this.page * this.countPerPage; i < this.countPerPage; i++) {
                if (i < this.filteredIndices.length) {
                    this.pageIndices.push(this.filteredIndices[i]);
                }
            }

            // The info text box
            this.info = 'Page ' +
                this.commaFormatted(this.page) + ' of ' +
                this.commaFormatted(this.numPages) + ' - ';
        },
        commaFormatted: function (x) {
            return x.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
        }
    },
    template: `
        <div class="page tweets">
            <h1>
                Choose which tweets should never get automatically deleted
                <img
                    class="refresh"
                    v-on:click="fetchTweets()"
                    src="/static/img/refresh.png"
                    alt="Refresh" title="Refresh" />
            </h1>

            <template v-if="loading">
                <p><img src="/static/img/loading.gif" alt="Loading" /></p>
            </template>
            <template v-else>
            <div class="controls">
                <div class="filter">
                    <input placeholder="Filter" type="text" v-bind:value="filterQuery" />
                </div>
                <div class="options">
                    <label><input type="checkbox" v-bind:checked="showReplies" /> Show replies</label>
                </div>
                <div class="info">{{ info }}</div>
                <div class="pagination"></div>
            </div>

                <ul v-for="id in pageIndices">
                    <tweet v-bind:tweet="tweets[id]"></tweet>
                </ul>
            </template>
        </div>
    `
})